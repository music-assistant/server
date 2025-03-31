"""
Spotify Connect plugin for Music Assistant.

We tie a single player to a single Spotify Connect daemon.
The provider has multi instance support,
so multiple players can be linked to multiple Spotify Connect daemons.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from aiohttp.web import Response
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    EventType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.models.plugin import PluginProvider, PluginSource
from music_assistant.providers.spotify.helpers import get_librespot_binary

if TYPE_CHECKING:
    from aiohttp.web import Request
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_HANDOFF_MODE = "handoff_mode"
CONNECT_ITEM_ID = "spotify_connect"

EVENTS_SCRIPT = pathlib.Path(__file__).parent.resolve().joinpath("events.py")


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SpotifyConnectProvider(mass, manifest, config)


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
            description="Select the player for which you want to enable Spotify Connect.",
            multi_value=False,
            options=[
                ConfigValueOption(x.display_name, x.player_id)
                for x in mass.players.all(False, False)
            ],
            required=True,
        ),
        # ConfigEntry(
        #     key=CONF_HANDOFF_MODE,
        #     type=ConfigEntryType.BOOLEAN,
        #     label="Enable handoff mode",
        #     default_value=False,
        #     description="The default behavior of the Spotify Connect plugin is to "
        #     "forward the actual Spotify Connect audio stream as-is to the player. "
        #     "The Spotify audio is basically just a live audio stream. \n\n"
        #     "For controlling the playback (and queue contents), "
        #     "you need to use the Spotify app. Also, depending on the player's "
        #     "buffering strategy and capabilities, the audio may not be fully in sync with "
        #     "what is shown in the Spotify app. \n\n"
        #     "When enabling handoff mode, the Spotify Connect plugin will instead "
        #     "forward the Spotify playback request to the Music Assistant Queue, so basically "
        #     "the spotify app can be used to initiate playback, but then MA will take over "
        #     "the playback and manage the queue, which is the normal operating mode of MA. \n\n"
        #     "This mode however means that the Spotify app will not report the actual playback ",
        #     required=False,
        # ),
    )


class SpotifyConnectProvider(PluginProvider):
    """Implementation of a Spotify Connect Plugin."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self.mass_player_id = cast("str", self.config.get_value(CONF_MASS_PLAYER_ID))
        self.cache_dir = os.path.join(self.mass.cache_path, self.instance_id)
        self._librespot_bin: str | None = None
        self._stop_called: bool = False
        self._runner_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._librespot_proc: AsyncProcess | None = None
        self._librespot_started = asyncio.Event()
        self.named_pipe = f"/tmp/{self.instance_id}"  # noqa: S108
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.manifest.name,
            # we set passive to true because we
            # dont allow this source to be selected directly
            passive=True,
            # TODO: implement controlling spotify from MA itself
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
            metadata=PlayerMedia(
                "Spotify Connect",
            ),
            stream_type=StreamType.NAMED_PIPE,
            path=self.named_pipe,
        )
        self._audio_buffer: asyncio.Queue[bytes] = asyncio.Queue(10)
        self._on_unload_callbacks: list[Callable[..., None]] = [
            self.mass.subscribe(
                self._on_mass_player_event,
                (EventType.PLAYER_ADDED, EventType.PLAYER_REMOVED),
                id_filter=self.mass_player_id,
            ),
            self.mass.streams.register_dynamic_route(
                f"/{self.instance_id}",
                self._handle_custom_webservice,
            ),
        ]

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.AUDIO_SOURCE}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._librespot_bin = await get_librespot_binary()
        self.player = self.mass.players.get(self.mass_player_id)
        if self.player:
            self._setup_player_daemon()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task
        for callback in self._on_unload_callbacks:
            callback()

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    async def _librespot_runner(self) -> None:
        """Run the spotify connect daemon in a background task."""
        assert self._librespot_bin
        self.logger.info("Starting Spotify Connect background daemon")
        os.environ["MASS_CALLBACK"] = f"{self.mass.streams.base_url}/{self.instance_id}"
        await check_output("rm", "-f", self.named_pipe)
        await asyncio.sleep(0.1)
        await check_output("mkfifo", self.named_pipe)
        await asyncio.sleep(0.1)
        try:
            args: list[str] = [
                self._librespot_bin,
                "--name",
                self.name,
                "--cache",
                self.cache_dir,
                "--disable-audio-cache",
                "--bitrate",
                "320",
                "--backend",
                "pipe",
                "--device",
                self.named_pipe,
                "--dither",
                "none",
                # disable volume control
                "--mixer",
                "softvol",
                "--volume-ctrl",
                "fixed",
                "--initial-volume",
                f"{self.player.volume_level if self.player and self.player.volume_level else 100}",
                "--enable-volume-normalisation",
                # forward events to the events script
                "--onevent",
                str(EVENTS_SCRIPT),
                "--emit-sink-events",
            ]
            self._librespot_proc = librespot = AsyncProcess(
                args, stdout=False, stderr=True, name=f"librespot[{self.name}]"
            )
            await librespot.start()

            # keep reading logging from stderr until exit
            async for line in librespot.iter_stderr():
                if (
                    not self._librespot_started.is_set()
                    and "Using StdoutSink (pipe) with format: S16" in line
                ):
                    self._librespot_started.set()
                if "error sending packet Os" in line:
                    continue
                if "dropping truncated packet" in line:
                    continue
                if "couldn't parse packet from " in line:
                    continue
                self.logger.debug(line)
        finally:
            await librespot.close(True)
            self.logger.info("Spotify Connect background daemon stopped for %s", self.name)
            await check_output("rm", "-f", self.named_pipe)
            if not self._librespot_started.is_set():
                self.unload_with_error("Unable to initialize librespot daemon.")
            # auto restart if not stopped manually
            if not self._stop_called and self._librespot_started.is_set():
                self._setup_player_daemon()

    def _setup_player_daemon(self) -> None:
        """Handle setup of the spotify connect daemon for a player."""
        self._librespot_started.clear()
        self._runner_task = self.mass.create_task(self._librespot_runner())

    def _on_mass_player_event(self, event: MassEvent) -> None:
        """Handle incoming event from linked airplay player."""
        if event.object_id != self.mass_player_id:
            return
        if event.event == EventType.PLAYER_REMOVED:
            self._stop_called = True
            self.mass.create_task(self.unload())
            return
        if event.event == EventType.PLAYER_ADDED:
            self._setup_player_daemon()
            return

    async def _handle_custom_webservice(self, request: Request) -> Response:
        """Handle incoming requests on the custom webservice."""
        json_data = await request.json()
        self.logger.debug("Received metadata on webservice: \n%s", json_data)

        event_name = json_data.get("event")

        # handle session connected event
        # this player has become the active spotify connect player
        # we need to start the playback
        if event_name in ("sink", "playing") and (not self._source_details.in_use_by):
            # initiate playback by selecting this source on the default player
            self.logger.debug("Initiating playback on %s", self.mass_player_id)
            self.mass.create_task(
                self.mass.players.select_source(self.mass_player_id, self.instance_id)
            )
            self._source_details.in_use_by = self.mass_player_id

        # parse metadata fields
        if "common_metadata_fields" in json_data:
            uri = json_data["common_metadata_fields"].get("uri", "Unknown")
            title = json_data["common_metadata_fields"].get("name", "Unknown")
            if artists := json_data.get("track_metadata_fields", {}).get("artists"):
                artist = artists[0]
            else:
                artist = "Unknown"
            album = json_data["common_metadata_fields"].get("album", "Unknown")
            if images := json_data["common_metadata_fields"].get("covers"):
                image_url = images[0]
            else:
                image_url = None
            if self._source_details.metadata is None:
                self._source_details.metadata = PlayerMedia(uri, media_type=MediaType.TRACK)
            self._source_details.metadata.uri = uri
            self._source_details.metadata.title = title
            self._source_details.metadata.artist = artist
            self._source_details.metadata.album = album
            self._source_details.metadata.image_url = image_url

        if event_name == "volume_changed" and (volume := json_data.get("volume")):
            # Spotify Connect volume is 0-65535
            volume = int(int(volume) / 65535 * 100)
            try:
                await self.mass.players.cmd_volume_set(self.mass_player_id, volume)
            except UnsupportedFeaturedException:
                self.logger.debug(f"Player {self.mass_player_id} does not support volume control")

        return Response()
