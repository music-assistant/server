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
import hashlib
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
    MediaType,
    PlaybackState,
    ProviderFeature,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import (
    AudioError,
    MediaNotFoundError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    MediaItemImage,
    ProviderMapping,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW, VERBOSE_LOG_LEVEL
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.helpers.util import interface_name_for_ip
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.airplay_receiver.helpers import get_shairport_sync_binary
from music_assistant.providers.airplay_receiver.metadata import MetadataReader

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_AIRPLAY_NAME = "airplay_name"
DEFAULT_AIRPLAY_NAME = "Music Assistant"

# Special value for auto player selection
PLAYER_ID_AUTO = "__auto__"

SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# stable id for the single AudioSource this provider exposes;
# combined with the provider instance_id this forms the persistent uri
AUDIO_SOURCE_ID = "main"


def airplay_receiver_port(instance_id: str) -> int:
    """
    Return the AirPlay port used by a receiver instance.

    Deterministically derived from the instance id, so it stays the same across
    server restarts (Python's built-in ``hash()`` is salted per process).

    :param instance_id: The provider instance id of the AirPlay receiver.
    """
    digest = hashlib.md5(instance_id.encode(), usedforsecurity=False).hexdigest()
    return 7000 + int(digest, 16) % 1000


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AirPlayReceiverProvider(mass, manifest, config)


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
        # Use port 7000+ for AirPlay 2 compatibility, one unique port per instance.
        # The port must be stable across restarts: the AirPlay provider uses it to
        # recognize (and ignore) our own shairport-sync advertisement in discovery.
        self.airplay_port = airplay_receiver_port(self.instance_id)
        airplay_name = cast("str", self.config.get_value(CONF_AIRPLAY_NAME)) or self.name
        # _audio_format describes the original AirPlay source (ALAC at 44.1/16,
        # the protocol-native format AirPlay senders use) and is what we
        # advertise to clients for source-format display.
        self._audio_format = AudioFormat(
            content_type=ContentType.ALAC,
            codec_type=ContentType.ALAC,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        # _decoded_audio_format is what shairport-sync actually pipes into MA
        # after decoding the ALAC stream; the streams controller hands this to
        # ffmpeg as the input format so it can read the FIFO correctly.
        self._decoded_audio_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            codec_type=ContentType.PCM_S16LE,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
        self._stream_metadata = StreamMetadata(title=f"AirPlay | {airplay_name}")
        self._audio_source = AudioSource(
            item_id=AUDIO_SOURCE_ID,
            provider=self.instance_id,
            name=self.name,
            provider_mappings={
                ProviderMapping(
                    item_id=AUDIO_SOURCE_ID,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._audio_format,
                )
            },
            can_play_pause=False,
            can_seek=False,
            can_next_previous=False,
            exclusive=True,
            allow_external_trigger=True,
            # passive: only flows when an external AirPlay client is connected
            can_initiate=False,
        )
        # _in_use_by_queue: the queue currently streaming us. Claimed in
        # on_source_selected (NOT in get_stream_details — that path also runs
        # from queue preload, where claiming would block a later cross-queue
        # handoff). Released in on_source_unselected when the session id
        # matches, or in _clear_active_player on external session disconnect.
        self._in_use_by_queue: str | None = None
        # _active_session_id is the controller-provided token for the current
        # stream request — used to reject stale on_source_unselected callbacks
        # after a same-queue reconnect supersedes the previous request.
        self._active_session_id: str | None = None
        self._pending_stop_task: asyncio.Task[None] | None = None
        self._on_unload_callbacks: list[Callable[..., None]] = []
        self._runner_error_count = 0
        self._metadata_reader: MetadataReader | None = None
        self._first_volume_event_received = False  # Track if we've received the first volume event

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            CONF_ENTRY_WARN_PREVIEW,
            ConfigEntry(
                key=CONF_MASS_PLAYER_ID,
                type=ConfigEntryType.STRING,
                multi_value=False,
                default_value=PLAYER_ID_AUTO,
                options=[
                    ConfigValueOption(PLAYER_ID_AUTO),
                    *(
                        ConfigValueOption(x.player_id, title=x.display_name)
                        for x in sorted(
                            self.mass.players.all_players(False, False),
                            key=lambda p: p.display_name.lower(),
                        )
                    ),
                ],
                required=True,
            ),
            ConfigEntry(
                key=CONF_AIRPLAY_NAME,
                type=ConfigEntryType.STRING,
                default_value=DEFAULT_AIRPLAY_NAME,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._shairport_bin = await get_shairport_sync_binary()
        # Always start the daemon - we always have a default player configured
        self._setup_shairport_daemon()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        self._stop_called = True

        # Stop shairport-sync daemon
        await self._stop_shairport_daemon()

        # Cleanup callbacks
        for callback in self._on_unload_callbacks:
            callback()

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        return [self._audio_source]

    async def get_stream_details(self, source_id: str, queue_id: str) -> StreamDetails:
        """
        Return StreamDetails for streaming the AirPlay audio to a queue.

        Side-effect-free: ownership is claimed in on_source_selected (which the
        streams controller fires before this method on the actual stream
        request). Keeping this idempotent means preload paths like
        player_queues._load_item can fetch streamdetails without claiming the
        source and blocking a subsequent cross-queue handoff.

        Raises AudioError when no AirPlay client is currently connected.
        """
        if source_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {source_id}")
        if not self._active_player_id:
            raise AudioError(
                "AirPlay receiver has no active client — start playback from your "
                "AirPlay-capable device first"
            )
        return StreamDetails(
            provider=self.instance_id,
            item_id=source_id,
            audio_format=self._audio_format,
            decoded_audio_format=self._decoded_audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.NAMED_PIPE,
            path=self.audio_pipe.path,
            stream_metadata=self._stream_metadata,
        )

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: int | None = None,
    ) -> None:
        """
        Handle source control commands (no-op: AirPlay receiver is passive).

        The AudioSource advertises no control capabilities, so MA will not invoke
        any actions here. Override exists only to satisfy the contract.
        """
        del source_id, action

    @property
    def active_player_id(self) -> str | None:
        """Return the currently active player ID for this plugin."""
        return self._active_player_id

    async def on_source_selected(
        self,
        source_id: str,
        player_id: str,
        queue_id: str,
        stream_session_id: str,
    ) -> None:
        """Handle callback when this AudioSource is selected/started on a player."""
        if source_id != AUDIO_SOURCE_ID or not player_id:
            return

        # Cache the queue_id (user-facing MA player) rather than the protocol-
        # level player_id; protocol bridges (e.g. Sendspin's spb_…) can tear
        # down between streams and their ID is then invalid for play_media.
        active_player_id = queue_id

        # If there's already an active player and it's different, kick it out.
        # The lock claim a few lines below replaces the previous queue's claim;
        # the prior stream's on_source_unselected may fire later, but its
        # session-id guard keeps it from clobbering the new claim.
        if self._active_player_id and self._active_player_id != active_player_id:
            prev_player_id = self._active_player_id
            self.logger.info(
                "Source selected on player %s, stopping playback on %s",
                active_player_id,
                prev_player_id,
            )
            try:
                await self.mass.players.cmd_stop(prev_player_id)
            except Exception as err:
                self.logger.debug("Failed to stop previous player %s: %s", prev_player_id, err)

        # Claim ownership for this queue. The lock lives here (not in
        # get_stream_details) so preload paths can fetch streamdetails without
        # accidentally blocking a subsequent cross-queue handoff at the actual
        # stream request.
        self._in_use_by_queue = queue_id
        # Record this request's session id so a later on_source_unselected can
        # tell whether it is the live teardown or a stale callback from a
        # superseded same-queue request.
        self._active_session_id = stream_session_id

        # Update the active player
        self._active_player_id = active_player_id
        self.logger.debug("Active player set to: %s", active_player_id)

        # Only persist the selected player as the new default if not in auto mode
        if self._default_player_id != PLAYER_ID_AUTO:
            self._save_last_player_id(active_player_id)

    async def on_source_unselected(
        self, source_id: str, queue_id: str, stream_session_id: str
    ) -> None:
        """Release the queue-scoped exclusive claim when MA tears down the stream."""
        if source_id != AUDIO_SOURCE_ID:
            return
        # Reject stale callbacks: only release if this is still the active
        # session. A queue_id check alone is not sufficient — same-queue
        # reconnects (player drops + reopens the same stream URL before the
        # original request's finally fires) would otherwise let the old
        # request's late callback clear the live claim of the new stream.
        if self._active_session_id != stream_session_id:
            return
        self._active_session_id = None
        if self._in_use_by_queue == queue_id:
            self._in_use_by_queue = None

    async def resolve_image(self, path: str) -> bytes:
        """
        Resolve an image from an image path.

        This returns raw bytes of the cover art image received from AirPlay metadata.

        :param path: The image path including the current cover art content hash suffix.
        """
        if not (self._metadata_reader and self._metadata_reader.cover_art_bytes):
            return b""
        current_hash = hashlib.md5(
            self._metadata_reader.cover_art_bytes, usedforsecurity=False
        ).hexdigest()[:8]
        # Only serve when the suffix matches the current artwork's hash, so a
        # stale request can't cache new bytes under an old hash key.
        if path == f"cover_art_{current_hash}":
            return self._metadata_reader.cover_art_bytes
        return b""

    async def _stop_shairport_daemon(self) -> None:
        """
        Stop the shairport-sync daemon without unloading the provider.

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
            if self.mass.players.get_player(self._active_player_id):
                return self._active_player_id
            # Active player no longer exists, clear it
            self._active_player_id = None

        # Handle auto selection
        if self._default_player_id == PLAYER_ID_AUTO:
            all_players = list(self.mass.players.all_players(False, False))
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
        if self.mass.players.get_player(self._default_player_id):
            return self._default_player_id
        self.logger.warning(
            "Configured default player '%s' no longer exists", self._default_player_id
        )
        return None

    def _clear_active_player(self) -> None:
        """
        Clear the active player and revert to default if configured.

        Called when playback ends to reset the plugin state.
        """
        prev_player_id = self._active_player_id
        self._active_player_id = None
        self._in_use_by_queue = None
        self._active_session_id = None

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
        config_content = config_content.replace("{INTERFACE_LINE}", self._get_mdns_interface_line())

        # Set default volume based on default player's current volume if available
        # Convert player volume (0-100) to AirPlay volume (-30.0 to 0.0 dB)
        player_volume = 100  # Default to 100%
        if self._default_player_id and self._default_player_id != PLAYER_ID_AUTO:
            if _player := self.mass.players.get_player(self._default_player_id):
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

    def _get_mdns_interface_line(self) -> str:
        """
        Build the shairport-sync ``general.interface`` directive, or an empty string.

        When the stream server is bound to a specific interface (not 0.0.0.0), pin
        the AirPlay mDNS advertisement to that same interface so the receiver is
        announced on the intended network instead of an unrelated one (e.g. a
        Docker bridge). Returns an empty string to advertise on all interfaces.
        """
        bind_ip = self.mass.streams.bind_ip
        if not bind_ip or bind_ip == "0.0.0.0":
            return ""
        iface_name = interface_name_for_ip(bind_ip)
        if not iface_name:
            self.logger.debug(
                "No interface found for stream bind IP %s; advertising on all interfaces",
                bind_ip,
            )
            return ""
        return f'\tinterface = "{iface_name}";\n'

    async def _setup_pipes_and_config(self) -> None:
        """
        Set up named pipes and configuration file for shairport-sync.

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
        """
        Write silence to the audio pipe to unblock ffmpeg.

        When shairport-sync stops writing but ffmpeg is still reading,
        writing silence will cause ffmpeg to output a chunk, which lets the
        outer consumer make forward progress so the queue's cmd_stop can
        close the stream cleanly.

        We write enough silence to ensure ffmpeg outputs at least one chunk.
        PCM_S16LE format: 2 bytes per sample, 2 channels, 44100 Hz
        Writing 1 second of silence = 44100 * 2 * 2 = 176400 bytes
        """
        self.logger.debug("Writing silence to audio pipe to unblock stream")
        silence = b"\x00" * 176400  # 1 second of silence in PCM_S16LE stereo 44.1kHz
        await self.audio_pipe.write(silence)

    def _process_shairport_log_line(self, line: str) -> None:
        """
        Process a log line from shairport-sync stderr.

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
        """
        Handle metadata updates from shairport-sync.

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
        if "volume" in metadata and self._in_use_by_queue:
            self._handle_volume_change(metadata["volume"])

        # Update source metadata fields
        self._update_source_metadata(metadata)

        # Handle cover art updates
        self._update_cover_art(metadata)

        # Push the metadata update through to the active queue item's streamdetails
        if self._in_use_by_queue:
            self.mass.streams.update_stream_metadata(
                self._in_use_by_queue,
                AUDIO_SOURCE_ID,
                self.instance_id,
                self._stream_metadata,
            )

    def _handle_play_state_change(self, play_state: str) -> None:
        """
        Handle play state changes from sessioncontrol hooks.

        :param play_state: The new play state ("playing" or "stopped").
        """
        if play_state == "playing":
            # Reset volume event flag for new playback session
            self._first_volume_event_received = False
            # Initiate playback via the standard play_media flow on the target player
            if not self._in_use_by_queue:
                target_player_id = self._get_target_player_id()
                if target_player_id:
                    self.logger.info("Starting AirPlay playback on player %s", target_player_id)
                    self._active_player_id = target_player_id
                    self.mass.create_task(self._start_playback(target_player_id))
                else:
                    self.logger.warning(
                        "AirPlay playback started but no player available. "
                        "Start it from the Live Inputs browse view to pick a player."
                    )
        elif play_state == "stopped":
            self.logger.info("AirPlay playback stopped")
            # Reset volume event flag for next session
            self._first_volume_event_received = False
            # Get the current player before clearing
            current_player_id = self._in_use_by_queue
            # Clear active player state (also clears _in_use_by_queue)
            self._clear_active_player()
            # Write silence to the pipe so ffmpeg can produce a chunk and notice the
            # stream has stopped; the stop command below closes the generator path.
            self.mass.create_task(self._write_silence_to_unblock_stream())
            # Track the stop so a new session cannot overtake it.
            if current_player_id:
                self._pending_stop_task = self.mass.create_task(
                    self.mass.players.cmd_stop(current_player_id)
                )

    async def _start_playback(self, target_player_id: str) -> None:
        """Start playback after any pending stop completes."""
        pending_stop_task = self._pending_stop_task
        if pending_stop_task is not None:
            # Await (even if already done) so a failed stop's exception is retrieved,
            # and continue regardless of how it failed: a stop that can't complete must
            # not keep the next session from starting. The reference is cleared only
            # after the await so concurrent starts (rapid "playing" events before the
            # stream is claimed) all await the same stop instead of racing past it.
            try:
                await pending_stop_task
            except Exception as err:
                self.logger.warning("Failed to stop previous AirPlay playback: %s", err)
            # Don't clear a newer stop that replaced ours while we were awaiting.
            if self._pending_stop_task is pending_stop_task:
                self._pending_stop_task = None
        await self.mass.player_queues.play_media(target_player_id, str(self._audio_source.uri))

    def _handle_volume_change(self, volume: int) -> None:
        """
        Handle volume changes from AirPlay client (iOS/macOS device).

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

        # Type check: ensure we have a valid player ID; queue_id == player_id by convention
        player_id = self._in_use_by_queue
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
        """
        Update source metadata fields from AirPlay metadata.

        :param metadata: Dictionary containing metadata updates.
        """
        # Update individual metadata fields
        if "title" in metadata:
            self._stream_metadata.title = metadata["title"]

        if "artist" in metadata:
            self._stream_metadata.artist = metadata["artist"]

        if "album" in metadata:
            self._stream_metadata.album = metadata["album"]

        if "duration" in metadata:
            self._stream_metadata.duration = metadata["duration"]

        if "elapsed_time" in metadata:
            self._stream_metadata.elapsed_time = metadata["elapsed_time"]
            # Always set elapsed_time_last_updated to current time when we receive elapsed_time
            self._stream_metadata.elapsed_time_last_updated = time.time()

    def _update_cover_art(self, metadata: dict[str, Any]) -> None:
        """
        Update cover art image URL from AirPlay metadata.

        :param metadata: Dictionary containing metadata updates.
        """
        if (
            "cover_art_timestamp" in metadata
            and self._metadata_reader
            and self._metadata_reader.cover_art_bytes
        ):
            # Use a content hash in the path so each unique image gets its own
            # thumbnail cache entry (the thumbnail cache is keyed on provider+path).
            img_hash = hashlib.md5(
                self._metadata_reader.cover_art_bytes, usedforsecurity=False
            ).hexdigest()[:8]
            image = MediaItemImage(
                type=ImageType.THUMB,
                path=f"cover_art_{img_hash}",
                provider=self.instance_id,
                remotely_accessible=False,
            )
            self._stream_metadata.image_url = self.mass.metadata.get_image_url(image)
        elif self._metadata_reader and self._metadata_reader.cover_art_bytes:
            if not self._stream_metadata.image_url:
                img_hash = hashlib.md5(
                    self._metadata_reader.cover_art_bytes, usedforsecurity=False
                ).hexdigest()[:8]
                image = MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"cover_art_{img_hash}",
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
                self._stream_metadata.image_url = self.mass.metadata.get_image_url(image)
