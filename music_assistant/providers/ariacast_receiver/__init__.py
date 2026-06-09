"""AriaCast Receiver Plugin Provider."""

from __future__ import annotations

import asyncio
import hashlib
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
    MediaType,
    PlaybackState,
    ProviderFeature,
    SourceControl,
    StreamType,
)
from music_assistant_models.errors import AudioError, MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    AudioSource,
    MediaItemImage,
    ProviderMapping,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import CONF_ENTRY_WARN_PREVIEW
from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.plugin import PluginProvider

from .helpers import _get_binary_path

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_MASS_PLAYER_ID = "mass_player_id"


PLAYER_ID_AUTO = "__auto__"
SUPPORTED_FEATURES = {ProviderFeature.AUDIO_SOURCE}

# stable id for the single AudioSource this provider exposes;
# combined with the provider instance_id this forms the persistent uri
AUDIO_SOURCE_ID = "main"


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
    )


class AriaCastBridge(PluginProvider):
    """Bridge for the AriaCast Go Binary."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize AriaCast Receiver."""
        super().__init__(mass, manifest, config, SUPPORTED_FEATURES)
        self._default_player_id = str(config.get_value(CONF_MASS_PLAYER_ID))

        # Process
        self._binary_process: AsyncProcess | None = None

        # Internal State
        # _active_player_id remembers the player that last consumed our stream so
        # we can reclaim it when the external app resumes after a pause.
        self._active_player_id: str | None = None
        # _in_use_by_queue is the queue currently streaming us (set in
        # on_source_selected, used to detect stream cancellation from inside
        # get_audio_stream and to gate metadata pushes to the consumer queue).
        self._in_use_by_queue: str | None = None
        # _active_session_id is the controller-provided token for the current
        # stream request — used to reject stale on_source_unselected callbacks
        # after a same-queue reconnect supersedes the previous request.
        self._active_session_id: str | None = None
        # mutable metadata mirroring what we'll push out via stream_metadata
        # updates on the active queue's streamdetails
        self._stream_metadata = StreamMetadata(title="AriaCast Ready")
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

        self._audio_format = AudioFormat(
            content_type=ContentType.PCM_S16LE,
            sample_rate=48000,
            bit_depth=16,
            channels=2,
        )
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
            # Binary stops stdout writes when paused, but MA proxies the
            # play/pause/next/previous commands via on_source_control to the
            # binary's HTTP API.
            can_play_pause=True,
            can_seek=False,
            can_next_previous=True,
            exclusive=True,
            allow_external_trigger=True,
            # passive: only flows when an external Cast client is connected
            can_initiate=False,
        )

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

    async def get_audio_sources(self) -> list[AudioSource]:
        """Return the AudioSources this plugin currently exposes."""
        return [self._audio_source]

    async def get_stream_details(self, source_id: str, queue_id: str) -> StreamDetails:
        """Return StreamDetails for streaming the AriaCast audio to a queue.

        Side-effect-free: ownership is claimed in on_source_selected (which the
        streams controller fires before this method on the actual stream
        request). Keeping this idempotent means preload paths like
        player_queues._load_item can fetch streamdetails without claiming the
        source and blocking a subsequent cross-queue handoff.
        """
        if source_id != AUDIO_SOURCE_ID:
            raise MediaNotFoundError(f"Unknown AudioSource: {source_id}")
        if not self._binary_is_playing:
            raise AudioError(
                "AriaCast has no active Cast client — start playback from your "
                "Cast-capable device first"
            )
        return StreamDetails(
            provider=self.instance_id,
            item_id=source_id,
            audio_format=self._audio_format,
            media_type=MediaType.AUDIO_SOURCE,
            stream_type=StreamType.CUSTOM,
            stream_metadata=self._stream_metadata,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """Stream PCM audio frames from the binary's stdout pump."""
        consumer_queue = self._in_use_by_queue
        # Snapshot the active session id so a same-queue reconnect (which
        # refreshes _active_session_id but not _in_use_by_queue) supersedes
        # this stream: the loop exits and the finally release skips so it
        # doesn't clobber the new session's claim.
        captured_session_id = self._active_session_id
        self.logger.debug("Audio stream requested by queue %s", consumer_queue)

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
                # Stop if our exclusive lock was released (pause), another queue
                # took over (cross-queue handoff), or a same-queue reconnect
                # superseded this session (session id rolled forward).
                if (
                    self._in_use_by_queue != consumer_queue
                    or self._active_session_id != captured_session_id
                ):
                    self.logger.debug("Stream lock released or taken over, stopping stream")
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
            self.logger.debug("Audio stream ended for queue %s", consumer_queue)
            self.frame_queue.clear()
            # Guard release on BOTH queue id AND session id so a stale
            # generator teardown after a same-queue reconnect doesn't clear
            # the new session's claim.
            if (
                self._in_use_by_queue == consumer_queue
                and self._active_session_id == captured_session_id
            ):
                self._in_use_by_queue = None

    async def on_source_control(
        self,
        source_id: str,
        action: SourceControl,
        value: int | None = None,
    ) -> None:
        """Proxy playback control commands to the AriaCast binary HTTP API."""
        if source_id != AUDIO_SOURCE_ID:
            return
        if action == SourceControl.PLAY:
            await self._cmd_play()
        elif action == SourceControl.PAUSE:
            await self._cmd_pause()
        elif action == SourceControl.NEXT:
            await self._send_api_command("next")
        elif action == SourceControl.PREVIOUS:
            await self._send_api_command("previous")

    async def on_source_selected(
        self,
        source_id: str,
        player_id: str,
        queue_id: str,
        stream_session_id: str,
    ) -> None:
        """Handle manual selection from the MA UI."""
        if source_id != AUDIO_SOURCE_ID:
            return
        # Claim ownership for this queue. The lock lives here (not in
        # get_stream_details) so preload paths can fetch streamdetails without
        # accidentally blocking a subsequent cross-queue handoff at the actual
        # stream request. Overwriting any prior claim is intentional: the
        # previous stream's get_audio_stream loop notices the queue change and
        # exits cleanly.
        self._in_use_by_queue = queue_id
        # Record this request's session id so a later on_source_unselected can
        # tell whether it is the live teardown or a stale callback from a
        # superseded same-queue request.
        self._active_session_id = stream_session_id
        # cache the queue_id (== MA player), not the protocol-level player_id —
        # protocol bridges (e.g. Sendspin spb_…) can tear down between streams
        self._active_player_id = queue_id

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
        meta = self._stream_metadata

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

        # Push the update through the streamdetails layer if a queue is consuming us
        if self._in_use_by_queue:
            self.mass.streams.update_stream_metadata(
                self._in_use_by_queue, AUDIO_SOURCE_ID, self.instance_id, meta
            )

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

        # The binary often sends a static local URL (like http://127.0.0.1/image/artwork).
        # We combine it with the track title to detect actual song/artwork changes.
        current_identifier = f"{artwork_url}_{meta.title}_{meta.artist}"

        last_artwork_identifier = getattr(self, "_last_artwork_identifier", None)
        if current_identifier != last_artwork_identifier:
            # New artwork detected
            self.logger.debug(
                "New artwork detected for track: %s (was: %s)", meta.title, last_artwork_identifier
            )
            self._last_artwork_identifier = current_identifier
            # Clear old artwork data to prevent serving stale image
            self._artwork_bytes = None
            if meta:
                meta.image_url = None
            self.mass.create_task(self._download_artwork())

    def _handle_playback_state_update(self, is_playing: bool) -> None:
        """Handle binary playback state and player management."""
        was_playing = self._binary_is_playing
        self.logger.debug(
            "Metadata update: is_playing=%s, was_playing=%s, active=%s, in_use_by_queue=%s",
            is_playing,
            was_playing,
            self._active_player_id,
            self._in_use_by_queue,
        )

        # Track binary state
        self._binary_is_playing = is_playing

        if is_playing and not self._in_use_by_queue:
            # Binary is playing but no queue is consuming the stream
            target = self._active_player_id or self._get_target_player_id()
            if target:
                self.logger.info("External playback started, routing to player %s", target)
                # Clear queue before resuming to remove old silence/data
                self.frame_queue.clear()
                self.frame_available.clear()
                self._active_player_id = target
                self.mass.create_task(
                    self.mass.player_queues.play_media(target, str(self._audio_source.uri))
                )
        elif not is_playing and was_playing and self._in_use_by_queue:
            # App paused playback - release the player so MA can play other content;
            # _active_player_id is preserved so resume can reclaim it.
            self.logger.info("External playback paused, releasing player")
            self._active_player_id = self._in_use_by_queue
            # Stopping the player closes our generator and clears _in_use_by_queue
            target_player = self._in_use_by_queue
            self.frame_queue.clear()
            self.frame_available.clear()
            self.mass.create_task(self.mass.players.cmd_stop(target_player))

    async def _cmd_play(self) -> None:
        """Send play command to the binary."""
        self.logger.info("PLAY command")

        # If player was released on pause, reclaim it via play_media
        if not self._in_use_by_queue and self._active_player_id:
            # Clear queue before resuming to remove old silence/data
            self.frame_queue.clear()
            self.frame_available.clear()
            await self.mass.player_queues.play_media(
                self._active_player_id, str(self._audio_source.uri)
            )

        await self._send_api_command("play")

    async def _cmd_pause(self) -> None:
        """Send pause command to the binary."""
        self.logger.info("PAUSE command")

        # Release the player (mirrors the external-pause path) - this makes MA show it as idle
        # Keep track of active_player_id so we can reclaim it on resume
        if self._in_use_by_queue:
            self._active_player_id = self._in_use_by_queue
            target_player = self._in_use_by_queue
            # Clear the frame queue to prevent old silence from being played on resume
            self.frame_queue.clear()
            self.frame_available.clear()
            await self.mass.players.cmd_stop(target_player)

        await self._send_api_command("pause")

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
                        # Use a content-derived hash to prevent unbounded cache growth
                        img_hash = hashlib.md5(img_data).hexdigest()[:8]
                        image = MediaItemImage(
                            type=ImageType.THUMB,
                            path=f"artwork_{img_hash}",
                            provider=self.instance_id,
                            remotely_accessible=False,
                        )

                        self._stream_metadata.image_url = self.mass.metadata.get_image_url(image)

                        if self._in_use_by_queue:
                            self.mass.streams.update_stream_metadata(
                                self._in_use_by_queue,
                                AUDIO_SOURCE_ID,
                                self.instance_id,
                                self._stream_metadata,
                            )
                else:
                    self.logger.warning("Failed to download artwork: HTTP %s", response.status)
        except Exception as e:
            self.logger.debug("Failed to download artwork: %s", e)

    async def resolve_image(self, path: str) -> bytes:
        """Return raw image bytes to Music Assistant."""
        if path.startswith("artwork") and self._artwork_bytes:
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
