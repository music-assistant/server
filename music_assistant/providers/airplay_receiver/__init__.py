"""
AirPlay Receiver plugin for Music Assistant.

This plugin allows Music Assistant to receive AirPlay audio streams
and use them as a source for any player. It uses shairport-sync to
receive the AirPlay streams and outputs them as PCM audio.

The provider has multi-instance support, so multiple AirPlay receivers
can be configured with different names.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.models.plugin import PluginProvider, PluginSource
from music_assistant.providers.airplay_receiver.helpers import get_shairport_sync_binary
from music_assistant.providers.airplay_receiver.metadata import MetadataReader

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_AIRPLAY_NAME = "airplay_name"

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AirPlayReceiverProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        CONF_ENTRY_WARN_PREVIEW,
        ConfigEntry(
            key=CONF_MASS_PLAYER_ID,
            type=ConfigEntryType.STRING,
            label="Connected Music Assistant Player",
            description="Select the player that will play the AirPlay audio stream.",
            multi_value=False,
            options=[
                ConfigValueOption(x.display_name, x.player_id)
                for x in sorted(
                    mass.players.all(False, False), key=lambda p: p.display_name.lower()
                )
            ],
            required=True,
        ),
        ConfigEntry(
            key=CONF_AIRPLAY_NAME,
            type=ConfigEntryType.STRING,
            label="AirPlay Device Name",
            description="How should this AirPlay receiver be named in the AirPlay device list?",
            default_value="Music Assistant",
        ),
    )


class AirPlayReceiverProvider(PluginProvider):
    """Implementation of an AirPlay Receiver Plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self.mass_player_id = cast("str", self.config.get_value(CONF_MASS_PLAYER_ID))
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self._shairport_bin: str | None = None
        self._stop_called: bool = False
        self._runner_task: asyncio.Task[None] | None = None
        self._shairport_proc: AsyncProcess | None = None
        self._shairport_started = asyncio.Event()
        self.audio_pipe = f"/tmp/ma_airplay_audio_{self.instance_id}"  # noqa: S108
        self.metadata_pipe = f"/tmp/ma_airplay_metadata_{self.instance_id}"  # noqa: S108
        airplay_name = cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.name,
            # Set passive to true because we don't allow this source to be selected directly
            # It will be automatically selected when AirPlay playback starts
            passive=True,
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                codec_type=ContentType.PCM_S16LE,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
            metadata=StreamMetadata(
                title=f"AirPlay | {airplay_name}",
            ),
            stream_type=StreamType.NAMED_PIPE,
            path=self.audio_pipe,
        )
        self._on_unload_callbacks: list[Callable[..., None]] = []
        self._runner_error_count = 0
        self._metadata_reader: MetadataReader | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._shairport_bin = await get_shairport_sync_binary()
        self.player = self.mass.players.get(self.mass_player_id)
        if self.player:
            self._setup_shairport_daemon()

        # Subscribe to events
        self._on_unload_callbacks.append(
            self.mass.subscribe(
                self._on_mass_player_event,
                (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED),
                id_filter=self.mass_player_id,
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True

        # Stop metadata reader
        if self._metadata_reader:
            await self._metadata_reader.stop()

        # Stop shairport-sync process
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task

        # Cleanup callbacks
        for callback in self._on_unload_callbacks:
            callback()

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    async def _shairport_runner(self) -> None:
        """Run the shairport-sync daemon in a background task."""
        assert self._shairport_bin
        self.logger.info("Starting AirPlay Receiver background daemon")

        # Clean up any existing pipes
        await check_output("rm", "-f", self.audio_pipe)
        await check_output("rm", "-f", self.metadata_pipe)
        await asyncio.sleep(0.1)

        # Create named pipes for audio and metadata
        await check_output("mkfifo", self.audio_pipe)
        await check_output("mkfifo", self.metadata_pipe)
        await asyncio.sleep(0.1)

        try:
            args: list[str] = [
                self._shairport_bin,
                "--name",
                cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name,
                "--output",
                "pipe",
                "--audio-backend-buffer-desired-length",
                "0.2",
                "--audio-backend-latency-offset",
                "0",
                # Output audio to pipe
                "--output-device",
                self.audio_pipe,
                # Enable metadata
                "--metadata-enable",
                "--metadata-pipename",
                self.metadata_pipe,
                # Get cover art
                "--get-coverart",
            ]

            self._shairport_proc = shairport = AsyncProcess(
                args, stdout=False, stderr=True, name=f"shairport-sync[{self.name}]"
            )
            await shairport.start()

            # Start metadata reader
            self._metadata_reader = MetadataReader(
                self.metadata_pipe, self.logger, self._on_metadata_update
            )
            await self._metadata_reader.start()

            # Keep reading logging from stderr until exit
            async for line in shairport.iter_stderr():
                if not self._shairport_started.is_set() and "Play begins" in line:
                    self._shairport_started.set()
                if "Connection from" in line:
                    self.logger.info("AirPlay client connected: %s", line)
                if "Play begins" in line:
                    # Initiate playback by selecting this source on the default player
                    if not self._source_details.in_use_by:
                        self.mass.create_task(
                            self.mass.players.select_source(self.mass_player_id, self.instance_id)
                        )
                        self._source_details.in_use_by = self.mass_player_id
                if "Play stopped" in line:
                    self.logger.info("AirPlay playback stopped")
                self.logger.debug(line)

        finally:
            await shairport.close(True)
            self.logger.info("AirPlay Receiver background daemon stopped for %s", self.name)

            # Stop metadata reader
            if self._metadata_reader:
                await self._metadata_reader.stop()

            # Clean up pipes
            await check_output("rm", "-f", self.audio_pipe)
            await check_output("rm", "-f", self.metadata_pipe)

            if not self._shairport_started.is_set():
                self.unload_with_error("Unable to initialize shairport-sync daemon.")
            # Auto restart if not stopped manually
            elif not self._stop_called and self._runner_error_count >= 5:
                self.unload_with_error("shairport-sync daemon failed to start multiple times.")
            elif not self._stop_called:
                self._runner_error_count += 1
                self.mass.call_later(2, self._setup_shairport_daemon)

    def _setup_shairport_daemon(self) -> None:
        """Handle setup of the shairport-sync daemon for a player."""
        self._shairport_started.clear()
        self._runner_task = self.mass.create_task(self._shairport_runner())

    def _on_mass_player_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked player."""
        if event.object_id != self.mass_player_id:
            return
        if event.event == EventType.PLAYER_REMOVED:
            self._stop_called = True
            self.mass.create_task(self.unload())
            return
        if event.event == EventType.PLAYER_ADDED:
            self._setup_shairport_daemon()
            return

    def _on_metadata_update(self, metadata: dict[str, Any]) -> None:
        """Handle metadata updates from shairport-sync."""
        self.logger.debug("Received metadata update: %s", metadata)

        # Update source metadata
        if self._source_details.metadata is None:
            airplay_name = cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name
            self._source_details.metadata = StreamMetadata(title=f"AirPlay | {airplay_name}")

        if "title" in metadata:
            self._source_details.metadata.title = metadata["title"]

        if "artist" in metadata:
            self._source_details.metadata.artist = metadata["artist"]

        if "album" in metadata:
            self._source_details.metadata.album = metadata["album"]

        if "cover_art" in metadata:
            # Cover art is base64 encoded image data
            # We could save it to a file and provide a URL, but for now we'll skip it
            pass

        # Signal update to connected player
        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(self._source_details.in_use_by)
