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
import time
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    PlaybackState,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import UnsupportedFeaturedException
from music_assistant_models.media_items import AudioFormat, MediaItemImage
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW, VERBOSE_LOG_LEVEL
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.models.plugin import PluginProvider, PluginSource
from music_assistant.providers.airplay_receiver.helpers import get_shairport_sync_binary
from music_assistant.providers.airplay_receiver.metadata import MetadataReader

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_AIRPLAY_NAME = "airplay_name"
CONF_ALLOW_PLAYER_SWITCH = "allow_player_switch"

# Special value for auto player selection
PLAYER_ID_AUTO = "__auto__"

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
            description="The Music Assistant player connected to this AirPlay receiver plugin. "
            "When you stream audio via AirPlay to this virtual speaker, "
            "the audio will play on the selected player. "
            "Set to 'Auto' to automatically select a currently playing player, "
            "or the first available player if none is playing.",
            multi_value=False,
            default_value=PLAYER_ID_AUTO,
            options=[
                ConfigValueOption("Auto (prefer playing player)", PLAYER_ID_AUTO),
                *(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in sorted(
                        mass.players.all(False, False), key=lambda p: p.display_name.lower()
                    )
                ),
            ],
            required=True,
        ),
        ConfigEntry(
            key=CONF_ALLOW_PLAYER_SWITCH,
            type=ConfigEntryType.BOOLEAN,
            label="Allow manual player switching",
            description="When enabled, you can select this plugin as a source on any player "
            "to switch playback to that player. When disabled, playback is fixed to the "
            "configured default player.",
            default_value=True,
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
        # Default player ID from config (PLAYER_ID_AUTO or a specific player_id)
        self._default_player_id: str = (
            cast("str", self.config.get_value(CONF_MASS_PLAYER_ID)) or PLAYER_ID_AUTO
        )
        # Whether manual player switching is allowed (default to True for upgrades)
        allow_switch_value = self.config.get_value(CONF_ALLOW_PLAYER_SWITCH)
        self._allow_player_switch: bool = (
            cast("bool", allow_switch_value) if allow_switch_value is not None else True
        )
        # Currently active player (the one currently playing or selected)
        self._active_player_id: str | None = None
        self._shairport_bin: str | None = None
        self._stop_called: bool = False
        self._runner_task: asyncio.Task[None] | None = None
        self._shairport_proc: AsyncProcess | None = None
        self._shairport_started = asyncio.Event()
        # Initialize named pipe helpers
        audio_pipe_path = f"/tmp/ma_airplay_audio_{self.instance_id}"  # noqa: S108
        metadata_pipe_path = f"/tmp/ma_airplay_metadata_{self.instance_id}"  # noqa: S108
        self.audio_pipe = AsyncNamedPipeWriter(audio_pipe_path)
        self.metadata_pipe = AsyncNamedPipeWriter(metadata_pipe_path)
        self.config_file = f"/tmp/ma_shairport_sync_{self.instance_id}.conf"  # noqa: S108
        # Use port 7000+ for AirPlay 2 compatibility
        # Each instance gets a unique port: 7000, 7001, 7002, etc.
        self.airplay_port = 7000 + (hash(self.instance_id) % 1000)
        airplay_name = cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.name,
            # passive=False allows this source to be selected on any player
            # Only show in source list if player switching is allowed
            passive=not self._allow_player_switch,
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
            path=self.audio_pipe.path,
        )
        # Set the on_select callback for when the source is selected on a player
        self._source_details.on_select = self._on_source_selected
        self._on_unload_callbacks: list[Callable[..., None]] = []
        self._runner_error_count = 0
        self._metadata_reader: MetadataReader | None = None
        self._first_volume_event_received = False  # Track if we've received the first volume event

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._shairport_bin = await get_shairport_sync_binary()
        # Always start the daemon - we always have a default player configured
        self._setup_shairport_daemon()

    async def _stop_shairport_daemon(self) -> None:
        """Stop the shairport-sync daemon without unloading the provider.

        This allows the provider to restart shairport-sync later when needed.
        """
        # Stop metadata reader
        if self._metadata_reader:
            await self._metadata_reader.stop()
            self._metadata_reader = None

        # Stop shairport-sync process
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner_task
            self._runner_task = None

        # Reset the shairport process reference
        self._shairport_proc = None
        self._shairport_started.clear()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True

        # Stop shairport-sync daemon
        await self._stop_shairport_daemon()

        # Cleanup callbacks
        for callback in self._on_unload_callbacks:
            callback()

    def get_source(self) -> PluginSource:
        """Get (audio)source details for this plugin."""
        return self._source_details

    @property
    def active_player_id(self) -> str | None:
        """Return the currently active player ID for this plugin."""
        return self._active_player_id

    def _get_target_player_id(self) -> str | None:
        """
        Determine the target player ID for playback.

        Returns the player ID to use based on the following priority:
        1. If a player was explicitly selected (source selected on a player), use that
        2. If default is 'auto': prefer playing player, then first available
        3. If a specific default player is configured, use that

        :return: The player ID to use for playback, or None if no player available.
        """
        # If there's an active player (source was selected on a player), use it
        if self._active_player_id:
            # Validate that the active player still exists
            if self.mass.players.get(self._active_player_id):
                return self._active_player_id
            # Active player no longer exists, clear it
            self._active_player_id = None

        # Handle auto selection
        if self._default_player_id == PLAYER_ID_AUTO:
            all_players = list(self.mass.players.all(False, False))
            # First, try to find a playing player
            for player in all_players:
                if player.state.playback_state == PlaybackState.PLAYING:
                    self.logger.debug("Auto-selecting playing player: %s", player.display_name)
                    return player.player_id
            # Fallback to first available player
            if all_players:
                first_player = all_players[0]
                self.logger.debug(
                    "Auto-selecting first available player: %s", first_player.display_name
                )
                return first_player.player_id
            # No player available
            return None

        # Use the specific default player if configured and it still exists
        if self.mass.players.get(self._default_player_id):
            return self._default_player_id
        self.logger.warning(
            "Configured default player '%s' no longer exists", self._default_player_id
        )
        return None

    async def _on_source_selected(self) -> None:
        """
        Handle callback when this source is selected on a player.

        This is called by the player controller when a user selects this
        plugin as a source on a specific player.
        """
        # The player that selected us is stored in in_use_by by the player controller
        new_player_id = self._source_details.in_use_by
        if not new_player_id:
            return

        # Check if manual player switching is allowed
        if not self._allow_player_switch:
            # Player switching disabled - only allow if it matches the current target
            current_target = self._get_target_player_id()
            if new_player_id != current_target:
                self.logger.debug(
                    "Manual player switching disabled, ignoring selection on %s",
                    new_player_id,
                )
                # Revert in_use_by to reflect the rejection
                self._source_details.in_use_by = current_target
                self.mass.players.trigger_player_update(new_player_id)
                return

        # If there's already an active player and it's different, kick it out
        if self._active_player_id and self._active_player_id != new_player_id:
            self.logger.info(
                "Source selected on player %s, stopping playback on %s",
                new_player_id,
                self._active_player_id,
            )
            # Stop the current player
            try:
                await self.mass.players.cmd_stop(self._active_player_id)
            except Exception as err:
                self.logger.debug(
                    "Failed to stop previous player %s: %s", self._active_player_id, err
                )

        # Update the active player
        self._active_player_id = new_player_id
        self.logger.debug("Active player set to: %s", new_player_id)

        # Only persist the selected player as the new default if not in auto mode
        if self._default_player_id != PLAYER_ID_AUTO:
            self._save_last_player_id(new_player_id)

    def _clear_active_player(self) -> None:
        """
        Clear the active player and revert to default if configured.

        Called when playback ends to reset the plugin state.
        """
        prev_player_id = self._active_player_id
        self._active_player_id = None
        self._source_details.in_use_by = None

        if prev_player_id:
            self.logger.debug("Playback ended on player %s, clearing active player", prev_player_id)
            # Trigger update for the player that was using this source
            self.mass.players.trigger_player_update(prev_player_id)

    def _save_last_player_id(self, player_id: str) -> None:
        """Persist the selected player ID to config as the new default."""
        if self._default_player_id == player_id:
            return  # No change needed
        try:
            self.mass.config.set_raw_provider_config_value(
                self.instance_id, CONF_MASS_PLAYER_ID, player_id
            )
            self._default_player_id = player_id
        except Exception as err:
            self.logger.debug("Failed to persist player ID: %s", err)

    async def _create_config_file(self) -> None:
        """Create shairport-sync configuration file from template."""
        # Read template
        template_path = os.path.join(os.path.dirname(__file__), "bin", "shairport-sync.conf")

        def _read_template() -> str:
            with open(template_path, encoding="utf-8") as f:
                return f.read()

        template = await asyncio.to_thread(_read_template)

        # Replace placeholders
        airplay_name = cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name
        config_content = template.replace("{AIRPLAY_NAME}", airplay_name)
        config_content = config_content.replace("{METADATA_PIPE}", self.metadata_pipe.path)
        config_content = config_content.replace("{AUDIO_PIPE}", self.audio_pipe.path)
        config_content = config_content.replace("{PORT}", str(self.airplay_port))

        # Set default volume based on default player's current volume if available
        # Convert player volume (0-100) to AirPlay volume (-30.0 to 0.0 dB)
        player_volume = 100  # Default to 100%
        if self._default_player_id and self._default_player_id != PLAYER_ID_AUTO:
            if _player := self.mass.players.get(self._default_player_id):
                if _player.volume_level is not None:
                    player_volume = _player.volume_level
        # Map 0-100 to -30.0...0.0
        airplay_volume = (player_volume / 100.0) * 30.0 - 30.0
        config_content = config_content.replace("{DEFAULT_VOLUME}", f"{airplay_volume:.1f}")

        # Write config file
        def _write_config() -> None:
            with open(self.config_file, "w", encoding="utf-8") as f:
                f.write(config_content)

        await asyncio.to_thread(_write_config)

    async def _setup_pipes_and_config(self) -> None:
        """Set up named pipes and configuration file for shairport-sync.

        :raises: OSError if pipe or config file creation fails.
        """
        # Remove any existing pipes and config
        await self._cleanup_pipes_and_config()

        # Create named pipes for audio and metadata
        await self.audio_pipe.create()
        await self.metadata_pipe.create()

        # Create configuration file
        await self._create_config_file()

    async def _cleanup_pipes_and_config(self) -> None:
        """Clean up named pipes and configuration file."""
        await self.audio_pipe.remove()
        await self.metadata_pipe.remove()
        await check_output("rm", "-f", self.config_file)

    async def _write_silence_to_unblock_stream(self) -> None:
        """Write silence to the audio pipe to unblock ffmpeg.

        When shairport-sync stops writing but ffmpeg is still reading,
        writing silence will cause ffmpeg to output a chunk, which will
        then check in_use_by and break out of the loop.

        We write enough silence to ensure ffmpeg outputs at least one chunk.
        PCM_S16LE format: 2 bytes per sample, 2 channels, 44100 Hz
        Writing 1 second of silence = 44100 * 2 * 2 = 176400 bytes
        """
        self.logger.debug("Writing silence to audio pipe to unblock stream")
        silence = b"\x00" * 176400  # 1 second of silence in PCM_S16LE stereo 44.1kHz
        await self.audio_pipe.write(silence)

    def _process_shairport_log_line(self, line: str) -> None:
        """Process a log line from shairport-sync stderr.

        :param line: The log line to process.
        """
        # Check for fatal errors (log them, but process will exit on its own)
        if "fatal error:" in line.lower() or "unknown option" in line.lower():
            self.logger.error("Fatal error from shairport-sync: %s", line)
            return
        # Log connection messages at INFO level, everything else at DEBUG
        if "connection from" in line:
            self.logger.info("AirPlay client connected: %s", line)
        else:
            # Note: Play begin/stop events are now handled via sessioncontrol hooks
            # through the metadata pipe, so we don't need to parse stderr logs
            self.logger.debug(line)
        if not self._shairport_started.is_set():
            self._shairport_started.set()

    async def _shairport_runner(self) -> None:
        """Run the shairport-sync daemon in a background task."""
        assert self._shairport_bin
        self.logger.info("Starting AirPlay Receiver background daemon")
        await self._setup_pipes_and_config()

        try:
            args: list[str] = [
                self._shairport_bin,
                "--configfile",
                self.config_file,
            ]
            self._shairport_proc = shairport = AsyncProcess(
                args, stderr=True, name=f"shairport-sync[{self.name}]"
            )
            await shairport.start()

            # Check if process started successfully
            await asyncio.sleep(0.1)
            if shairport.returncode is not None:
                self.logger.error(
                    "shairport-sync exited immediately with code %s", shairport.returncode
                )
                return

            # Start metadata reader
            self._metadata_reader = MetadataReader(
                self.metadata_pipe.path, self.logger, self._on_metadata_update
            )
            await self._metadata_reader.start()

            # Keep reading logging from stderr until exit
            self.logger.debug("Starting to read shairport-sync stderr")
            async for stderr_line in shairport.iter_stderr():
                line = stderr_line.strip()
                self._process_shairport_log_line(line)

        finally:
            await shairport.close()
            self.logger.info(
                "AirPlay Receiver background daemon stopped for %s (exit code: %s)",
                self.name,
                shairport.returncode,
            )

            # Stop metadata reader
            if self._metadata_reader:
                await self._metadata_reader.stop()

            # Clean up pipes and config
            await self._cleanup_pipes_and_config()

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

    def _on_metadata_update(self, metadata: dict[str, Any]) -> None:
        """Handle metadata updates from shairport-sync.

        :param metadata: Dictionary containing metadata updates.
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Received metadata update: %s", metadata)

        # Handle play state changes from sessioncontrol hooks
        if "play_state" in metadata:
            self._handle_play_state_change(metadata["play_state"])
            return

        # Handle metadata start (new track starting)
        if "metadata_start" in metadata:
            return

        # Handle volume changes from AirPlay client
        if "volume" in metadata and self._source_details.in_use_by:
            self._handle_volume_change(metadata["volume"])

        # Update source metadata fields
        self._update_source_metadata(metadata)

        # Handle cover art updates
        self._update_cover_art(metadata)

        # Signal update to connected player
        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(self._source_details.in_use_by)

    def _handle_play_state_change(self, play_state: str) -> None:
        """Handle play state changes from sessioncontrol hooks.

        :param play_state: The new play state ("playing" or "stopped").
        """
        if play_state == "playing":
            # Reset volume event flag for new playback session
            self._first_volume_event_received = False
            # Initiate playback by selecting this source on the target player
            if not self._source_details.in_use_by:
                target_player_id = self._get_target_player_id()
                if target_player_id:
                    self.logger.info("Starting AirPlay playback on player %s", target_player_id)
                    self._active_player_id = target_player_id
                    self.mass.create_task(
                        self.mass.players.select_source(target_player_id, self.instance_id)
                    )
                    self._source_details.in_use_by = target_player_id
                else:
                    self.logger.warning(
                        "AirPlay playback started but no player available. "
                        "Select this source on a player to start playback."
                    )
        elif play_state == "stopped":
            self.logger.info("AirPlay playback stopped")
            # Reset volume event flag for next session
            self._first_volume_event_received = False
            # Get the current player before clearing
            current_player_id = self._source_details.in_use_by
            # Clear active player state
            self._clear_active_player()
            # Write silence to the pipe to unblock ffmpeg
            # This will cause ffmpeg to output a chunk, which will then check in_use_by
            # and break out of the loop when it sees it's None
            self.mass.create_task(self._write_silence_to_unblock_stream())
            # Deselect source from player if there was one
            if current_player_id:
                self.mass.create_task(self.mass.players.select_source(current_player_id, None))

    def _handle_volume_change(self, volume: int) -> None:
        """Handle volume changes from AirPlay client (iOS/macOS device).

        ignore_volume_control = "yes" means shairport-sync doesn't do software volume control,
        but we still receive volume level changes from the client to apply to the player.

        :param volume: The new volume level (0-100).
        """
        # Skip the first volume event as it's the initial sync from default_airplay_volume
        # We don't want to override the player's current volume on startup
        if not self._first_volume_event_received:
            self._first_volume_event_received = True
            self.logger.debug(
                "Received initial AirPlay volume (%s%%), skipping to preserve player volume",
                volume,
            )
            return

        # Type check: ensure we have a valid player ID
        player_id = self._source_details.in_use_by
        if not player_id:
            return

        self.logger.debug(
            "AirPlay client volume changed to %s%%, applying to player %s",
            volume,
            player_id,
        )
        try:
            self.mass.create_task(self.mass.players.cmd_volume_set(player_id, volume))
        except UnsupportedFeaturedException:
            self.logger.debug("Player %s does not support volume control", player_id)

    def _update_source_metadata(self, metadata: dict[str, Any]) -> None:
        """Update source metadata fields from AirPlay metadata.

        :param metadata: Dictionary containing metadata updates.
        """
        # Initialize metadata if needed
        if self._source_details.metadata is None:
            airplay_name = cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name
            self._source_details.metadata = StreamMetadata(title=f"AirPlay | {airplay_name}")

        # Update individual metadata fields
        if "title" in metadata:
            self._source_details.metadata.title = metadata["title"]

        if "artist" in metadata:
            self._source_details.metadata.artist = metadata["artist"]

        if "album" in metadata:
            self._source_details.metadata.album = metadata["album"]

        if "duration" in metadata:
            self._source_details.metadata.duration = metadata["duration"]

        if "elapsed_time" in metadata:
            self._source_details.metadata.elapsed_time = metadata["elapsed_time"]
            # Always set elapsed_time_last_updated to current time when we receive elapsed_time
            self._source_details.metadata.elapsed_time_last_updated = time.time()

    def _update_cover_art(self, metadata: dict[str, Any]) -> None:
        """Update cover art image URL from AirPlay metadata.

        :param metadata: Dictionary containing metadata updates.
        """
        # Ensure metadata is initialized
        if self._source_details.metadata is None:
            return

        if "cover_art_timestamp" in metadata:
            # Use timestamp as query parameter to create a unique URL for each cover art update
            # This prevents browser caching issues when switching between tracks
            timestamp = metadata["cover_art_timestamp"]
            # Build image proxy URL for the cover art
            # The actual image bytes are stored in the metadata reader
            image = MediaItemImage(
                type=ImageType.THUMB,
                path="cover_art",
                provider=self.instance_id,
                remotely_accessible=False,
            )
            base_url = self.mass.metadata.get_image_url(image)
            # Append timestamp as query parameter for cache-busting
            self._source_details.metadata.image_url = f"{base_url}&t={timestamp}"
        elif self._metadata_reader and self._metadata_reader.cover_art_bytes:
            # Maintain image URL if we have cover art but didn't receive it in this update
            # This ensures the image URL persists across metadata updates
            if not self._source_details.metadata.image_url:
                # Generate timestamp for cache-busting even in fallback case
                timestamp = str(int(time.time() * 1000))
                image = MediaItemImage(
                    type=ImageType.THUMB,
                    path="cover_art",
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
                base_url = self.mass.metadata.get_image_url(image)
                self._source_details.metadata.image_url = f"{base_url}&t={timestamp}"

    async def resolve_image(self, path: str) -> bytes:
        """Resolve an image from an image path.

        This returns raw bytes of the cover art image received from AirPlay metadata.

        :param path: The image path (should be "cover_art" for AirPlay cover art).
        """
        if path == "cover_art" and self._metadata_reader and self._metadata_reader.cover_art_bytes:
            return self._metadata_reader.cover_art_bytes
        # Return empty bytes if no cover art is available
        return b""
