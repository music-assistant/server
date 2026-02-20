"""AriaCast Receiver Plugin Provider."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import ClientTimeout
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    PlaybackState,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import AudioFormat, MediaItemImage
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.plugin import PluginProvider, PluginSource

from .helpers import _get_binary_path

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"
CONF_ALLOW_PLAYER_SWITCH = "allow_player_switch"


PLAYER_ID_AUTO = "__auto__"
SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AriaCastBridge(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        CONF_ENTRY_WARN_PREVIEW,
        ConfigEntry(
            key=CONF_MASS_PLAYER_ID,
            type=ConfigEntryType.STRING,
            label="Connected Music Assistant Player",
            description="The player to use for playback.",
            default_value=PLAYER_ID_AUTO,
            options=[
                ConfigValueOption("Auto (prefer playing player)", PLAYER_ID_AUTO),
                *(
                    ConfigValueOption(x.display_name, x.player_id)
                    for x in sorted(
                        mass.players.all_players(False, False), key=lambda p: p.display_name.lower()
                    )
                ),
            ],
            required=True,
        ),
        ConfigEntry(
            key=CONF_ALLOW_PLAYER_SWITCH,
            type=ConfigEntryType.BOOLEAN,
            label="Allow manual player switching",
            default_value=True,
        ),
    )


class AriaCastBridge(PluginProvider):
    """Bridge for the AriaCast Go Binary."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize AriaCast Receiver."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self._default_player_id = str(config.get_value(CONF_MASS_PLAYER_ID))
        self._allow_player_switch = bool(config.get_value(CONF_ALLOW_PLAYER_SWITCH))

        # Process
        self._binary_process: AsyncProcess | None = None

        # Internal State
        self._active_player_id: str | None = None
        self._metadata_task: asyncio.Task[None] | None = None
        self._stdout_reader_task: asyncio.Task[None] | None = None
        self._stop_called = False
        self._binary_is_playing: bool = False  # Track binary playback state
        self._current_track_title: str | None = None  # Track song changes

        # Audio buffer - larger for high-latency players like Sendspin
        self.max_frames = 75  # 1.5 second buffer (75 frames * 20ms each)
        self.frame_queue: deque[bytes] = deque(maxlen=self.max_frames)
        self.frame_available = asyncio.Event()
        self._buffering = True  # Start in buffering mode

        # Artwork storage
        self._artwork_bytes: bytes | None = None
        self._artwork_timestamp: int = 0

        # Define the Source
        self._source_details = PluginSource(
            id=self.instance_id,
            name=self.name,
            passive=not self._allow_player_switch,
            can_play_pause=True,  # Binary stops stdout writes when paused
            can_seek=False,
            can_next_previous=True,
            audio_format=AudioFormat(
                content_type=ContentType.PCM_S16LE,
                sample_rate=48000,
                bit_depth=16,
                channels=2,
            ),
            metadata=StreamMetadata(title="AriaCast Ready"),
            stream_type=StreamType.CUSTOM,
        )

        # Bind Hooks
        self._source_details.on_select = self._on_source_selected
        self._source_details.on_play = self._cmd_play
        self._source_details.on_pause = self._cmd_pause
        self._source_details.on_next = self._cmd_next
        self._source_details.on_previous = self._cmd_previous

    async def handle_async_init(self) -> None:
        """Start the provider."""
        # Launch Binary with stdout and stderr mode
        binary_path = await asyncio.to_thread(_get_binary_path)
        args = [binary_path, "--stdout"]

        self.logger.info("Starting AriaCast binary: %s", binary_path)
        self._binary_process = AsyncProcess(args, name="ariacast", stdout=True, stderr=True)
        await self._binary_process.start()

        # Start Metadata Monitor
        self._metadata_task = self.mass.create_task(self._monitor_metadata())

        # Start Stdout Reader (feeds the frame queue)
        self._stdout_reader_task = self.mass.create_task(self._read_stdout_to_queue())

        # Start Stderr Reader (logging)
        self.mass.create_task(self._read_stderr())

    async def unload(self, is_removed: bool = False) -> None:
        """Cleanup resources."""
        self._stop_called = True

        if self._metadata_task:
            self._metadata_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._metadata_task

        if self._stdout_reader_task:
            self._stdout_reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._stdout_reader_task

        if self._binary_process:
            self.logger.info("Stopping AriaCast binary...")
            await self._binary_process.close()

    def get_source(self) -> PluginSource:
        """Return the plugin source details."""
        return self._source_details

    async def _monitor_metadata(self) -> None:
        """Connect to local Go binary WebSocket to receive metadata updates."""
        url = "ws://127.0.0.1:12889/metadata"
        retry_delay = 1

        while not self._stop_called:
            try:
                async with self.mass.http_session.ws_connect(url, heartbeat=30) as ws:
                    self.logger.info("Connected to AriaCast metadata stream")
                    retry_delay = 1  # Reset delay on success
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = msg.json()
                            if payload.get("type") == "metadata":
                                self._update_metadata(payload.get("data", {}))
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
            except Exception as exc:
                if not self._stop_called:
                    self.logger.debug(
                        "WebSocket connection to AriaCast metadata failed: %s. Retrying in %d s...",
                        exc,
                        retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)

    def _update_metadata(self, data: dict[str, Any]) -> None:
        """Update Music Assistant metadata from Go binary data."""
        if not self._source_details.metadata:
            self._source_details.metadata = StreamMetadata(title="AriaCast Ready")

        meta = self._source_details.metadata

        # Detect song change and clear queue to prevent stale audio
        new_title = data.get("title", "Unknown")
        self._handle_track_change(new_title)

        meta.title = new_title
        meta.artist = data.get("artist", "Unknown")
        meta.album = data.get("album", "Unknown")

        # Handle artwork
        self._handle_artwork_update(data.get("artwork_url"), meta)

        # Duration & Progress
        if duration_ms := data.get("duration_ms"):
            meta.duration = int(duration_ms / 1000)

        if position_ms := data.get("position_ms"):
            meta.elapsed_time = int(position_ms / 1000)
            meta.elapsed_time_last_updated = time.time()

        # Handle playback state
        self._handle_playback_state_update(data.get("is_playing", False))

        # Trigger UI Update
        if self._source_details.in_use_by:
            self.mass.players.trigger_player_update(self._source_details.in_use_by)

    def _handle_track_change(self, new_title: str) -> None:
        """Handle track change detection and queue clearing."""
        if self._current_track_title and new_title != self._current_track_title:
            if self._binary_is_playing:  # Only clear on song change during playback
                self.logger.info(
                    "Song changed from '%s' to '%s' - clearing audio queue",
                    self._current_track_title,
                    new_title,
                )
                self.frame_queue.clear()
                self.frame_available.clear()
        self._current_track_title = new_title

    def _handle_artwork_update(self, artwork_url: str | None, meta: StreamMetadata) -> None:
        """Handle artwork detection and download."""
        if not artwork_url:
            return

        last_artwork_identifier = getattr(self, "_last_artwork_identifier", None)
        if artwork_url != last_artwork_identifier:
            # New artwork detected
            self.logger.debug(
                "New artwork detected: %s (was: %s)", artwork_url, last_artwork_identifier
            )
            self._last_artwork_identifier = artwork_url
            # Clear old artwork data to prevent serving stale image
            self._artwork_bytes = None
            if meta:
                meta.image_url = None
            self.mass.create_task(self._download_artwork())

    def _handle_playback_state_update(self, is_playing: bool) -> None:
        """Handle binary playback state and player management."""
        was_playing = self._binary_is_playing
        self.logger.debug(
            "Metadata update: is_playing=%s, was_playing=%s, active=%s, in_use=%s",
            is_playing,
            was_playing,
            self._active_player_id,
            self._source_details.in_use_by,
        )

        # Track binary state
        self._binary_is_playing = is_playing

        if is_playing and not self._source_details.in_use_by:
            # Binary is playing but no player is consuming the stream
            if self._active_player_id:
                # Resume after pause - reclaim the same player
                self.logger.info(
                    "App resumed playback, reclaiming player %s", self._active_player_id
                )
                # Clear queue before resuming to remove old silence/data
                self.frame_queue.clear()
                self.frame_available.clear()
                self._source_details.in_use_by = self._active_player_id
                self.mass.players.trigger_player_update(self._active_player_id)
                self.mass.create_task(
                    self.mass.players.select_source(self._active_player_id, self.instance_id)
                )
            else:
                # First time playing - auto-select a player
                self._handle_auto_play()
        elif not is_playing and was_playing and self._source_details.in_use_by:
            # App paused playback - release the player
            self.logger.info("App paused playback, releasing player")
            self._active_player_id = self._source_details.in_use_by
            self._source_details.in_use_by = None
            # Clear queue to prevent old silence from accumulating
            self.frame_queue.clear()
            self.frame_available.clear()
            self.mass.players.trigger_player_update(self._active_player_id)

    def _handle_auto_play(self) -> None:
        """Automatically select a player when music starts."""
        target_id = self._get_target_player_id()
        if target_id:
            self._active_player_id = target_id
            self._source_details.in_use_by = target_id
            self.mass.create_task(self.mass.players.select_source(target_id, self.instance_id))

    # --- Command Wrappers ---

    async def _cmd_play(self) -> None:
        """Send play command."""
        self.logger.info("PLAY command")

        # If player was released on pause, reclaim it
        if not self._source_details.in_use_by and self._active_player_id:
            # Clear queue before resuming to remove old silence/data
            self.frame_queue.clear()
            self.frame_available.clear()
            self._source_details.in_use_by = self._active_player_id
            self.mass.players.trigger_player_update(self._active_player_id)
            # Restart playback on the player
            await self.mass.players.select_source(self._active_player_id, self.instance_id)

        await self._send_api_command("play")

    async def _cmd_pause(self) -> None:
        """Send pause command."""
        self.logger.info("PAUSE command")

        # Release the player (like Spotify Connect does) - this makes MA show it as idle
        # Keep track of active_player_id so we can reclaim it on resume
        if self._source_details.in_use_by:
            self._active_player_id = self._source_details.in_use_by
            self._source_details.in_use_by = None
            self.mass.players.trigger_player_update(self._active_player_id)

        # Clear the frame queue to prevent old silence from being played on resume
        self.frame_queue.clear()
        self.frame_available.clear()

        await self._send_api_command("pause")

    async def _cmd_next(self) -> None:
        """Send next-track command."""
        await self._send_api_command("next")

    async def _cmd_previous(self) -> None:
        """Send previous-track command."""
        await self._send_api_command("previous")

    async def _send_api_command(self, action: str) -> None:
        """Send control command (POST) using shared session."""
        url = "http://127.0.0.1:12889/api/command"
        try:
            async with self.mass.http_session.post(url, json={"action": action}) as response:
                body = await response.text()
                if not 200 <= response.status < 300:
                    self.logger.warning(
                        "Command '%s' failed with HTTP %s: %s",
                        action,
                        response.status,
                        body,
                    )
        except Exception as e:
            self.logger.warning("Failed to send command '%s': %s", action, e)

    async def _download_artwork(self) -> None:
        """Fetch artwork bytes from Go binary."""
        # Add a small delay to ensure binary has rotated the image
        await asyncio.sleep(0.2)
        artwork_url = "http://127.0.0.1:12889/image/artwork"
        self.logger.debug("Downloading artwork from %s", artwork_url)
        try:
            async with self.mass.http_session.get(
                artwork_url, timeout=ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    img_data = await response.read()
                    if img_data:
                        self._artwork_bytes = img_data
                        self._artwork_timestamp = int(time.time() * 1000)
                        self.logger.info(
                            "Artwork downloaded successfully, size: %d bytes", len(img_data)
                        )

                        image = MediaItemImage(
                            type=ImageType.THUMB,
                            path="artwork",
                            provider=self.instance_id,
                            remotely_accessible=False,
                        )
                        base_url = self.mass.metadata.get_image_url(image)

                        if self._source_details.metadata:
                            self._source_details.metadata.image_url = (
                                f"{base_url}&t={self._artwork_timestamp}"
                            )

                        if self._source_details.in_use_by:
                            self.mass.players.trigger_player_update(self._source_details.in_use_by)
                else:
                    self.logger.warning("Failed to download artwork: HTTP %s", response.status)
        except Exception as e:
            self.logger.debug("Failed to download artwork: %s", e)

    async def resolve_image(self, path: str) -> bytes:
        """Return raw image bytes to Music Assistant."""
        if path == "artwork" and self._artwork_bytes:
            return self._artwork_bytes
        return b""

    async def _read_stdout_to_queue(self) -> None:
        """Background task to read from binary stdout and populate frame queue."""
        frame_size = 3840  # 20ms of 48kHz stereo 16-bit

        if not self._binary_process:
            self.logger.error("Cannot read stdout: binary process not started")
            return

        self.logger.info("Starting to read audio from binary stdout")

        try:
            # Read from stdout in chunks
            while not self._stop_called:
                try:
                    # Read exactly one frame from stdout
                    data = await self._binary_process.read(frame_size)

                    if not data:
                        # Process ended or no more data
                        self.logger.debug("Stdout closed or no data")
                        break

                    if len(data) < frame_size:
                        # Incomplete frame, try to read remaining bytes
                        remaining = frame_size - len(data)
                        additional = await self._binary_process.read(remaining)
                        if additional:
                            data += additional

                    # Add to queue
                    self.frame_queue.append(data)
                    self.frame_available.set()

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.debug("Error reading from stdout: %s", e)
                    await asyncio.sleep(0.1)

        except Exception as e:
            self.logger.error("Fatal error in stdout reader: %s", e)
        finally:
            self.logger.info("Stdout reader task ended")

    async def _read_stderr(self) -> None:
        """Log errors from binary stderr."""
        if not self._binary_process:
            return
        async for line in self._binary_process.iter_stderr():
            self.logger.debug("[%s stderr] %s", self.name, line)

    async def get_audio_stream(self, player_id: str) -> AsyncGenerator[bytes, None]:
        """Return the custom audio stream for this source (like original ariacast_receiver)."""
        self.logger.debug("Audio stream requested by player %s", player_id)

        # Pre-buffering phase for high-latency players
        min_buffer_size = int(self.max_frames * 0.6)  # Wait for 60% full buffer
        self.logger.info("Pre-buffering: waiting for %d frames...", min_buffer_size)

        buffer_start = time.time()
        while len(self.frame_queue) < min_buffer_size and not self._stop_called:
            if time.time() - buffer_start > 5:  # Timeout after 5 seconds
                self.logger.warning(
                    "Pre-buffering timeout, starting with %d frames", len(self.frame_queue)
                )
                break
            await asyncio.sleep(0.05)

        self.logger.info("Starting playback with %d frames buffered", len(self.frame_queue))

        # Stream audio frames from the queue until playback stops
        try:
            while not self._stop_called:
                # Stop if player was released (pause) or changed
                if self._source_details.in_use_by != player_id:
                    self.logger.debug("Player released or changed, stopping stream")
                    break

                if self.frame_queue:
                    try:
                        frame = self.frame_queue.popleft()
                        yield frame
                    except IndexError:
                        # Queue became empty between the check and the pop
                        continue
                else:
                    # No data available, wait for new frames or stop
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.frame_available.wait(), timeout=1.0)
                        # Only clear the event if the queue is still empty
                        if not self.frame_queue:
                            self.frame_available.clear()
        finally:
            self.logger.debug("Audio stream ended for player %s", player_id)
            self.frame_queue.clear()

    # --- Helpers ---

    def _get_target_player_id(self) -> str | None:
        """Find the best player to use."""
        if self._active_player_id:
            if self.mass.players.get_player(self._active_player_id):
                return self._active_player_id
            self._active_player_id = None

        if self._default_player_id == PLAYER_ID_AUTO:
            for player in self.mass.players.all_players(False, False):
                if player.state.playback_state == PlaybackState.PLAYING:
                    return player.player_id
            players = list(self.mass.players.all_players(False, False))
            return players[0].player_id if players else None

        return str(self._default_player_id)

    async def _on_source_selected(self) -> None:
        """Handle manual selection in UI."""
        new_player_id = self._source_details.in_use_by
        if not new_player_id:
            return

        # Check if manual player switching is allowed
        if not self._allow_player_switch:
            current_target = self._get_target_player_id()
            if new_player_id != current_target:
                self.logger.debug(
                    "Manual player switching disabled, ignoring selection on %s",
                    new_player_id,
                )
                # Revert in_use_by
                self._source_details.in_use_by = current_target
                self.mass.players.trigger_player_update(new_player_id)
                return

        self._active_player_id = new_player_id
