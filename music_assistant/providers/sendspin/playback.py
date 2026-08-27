"""Playback session coordinator for Sendspin players."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from aiosendspin.models.types import AudioCodec as SendspinAudioCodec
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.push_stream import MAIN_CHANNEL, PushStream, StreamStoppedError
from aiosendspin.server.roles.player.v1 import PlayerV1Role
from music_assistant_models.enums import ContentType, MediaType
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.constants import CONF_OUTPUT_CHANNELS
from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.helpers.audio import iter_pcm_slices
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.helpers.util import import_module_in_thread
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)

if TYPE_CHECKING:
    from music_assistant.helpers.dsp import ComplexFilter

    from .player import SendspinPlayer
    from .provider import SendspinProvider


# Default session PCM format (MA-side and wire) used until _run_playback picks a
# leader-driven rate. Same sample format expressed in both MA and Sendspin types.
_DEFAULT_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_F32LE,
    sample_rate=48000,
    bit_depth=32,
    channels=2,
)
_DEFAULT_SENDSPIN_PCM_FORMAT = SendspinAudioFormat(
    sample_rate=48000,
    bit_depth=32,
    channels=2,
    sample_type="float",
)
# Media types whose upstream always feeds at realtime rate, so the Sendspin queue
# cannot grow after playback begins and their send-ahead stays at the min_buffer_ms
# floor. Buffered types (tracks, podcasts, etc.) race ahead and fill the queue
# naturally, so their send-ahead may extend to a larger required_lead_time_ms without
# lasting cost. Media type alone does not settle it: a track can come from a source
# that also feeds just-in-time, which is what StreamDetails.is_realtime marks - see
# _is_live_source().
_LIVE_MEDIA_TYPES: frozenset[MediaType] = frozenset(
    {
        MediaType.RADIO,
        MediaType.AUDIO_SOURCE,
        MediaType.PLUGIN_SOURCE,
    }
)


# Sample rate ceiling for lossy output codecs — anything above is wasted bandwidth.
_LOSSY_MAX_SAMPLE_RATE = 48000
# Max PCM slice fed to the producer per iteration.
_PRODUCER_SLICE_US = 100_000
# Max pending chunks between producer and committer before the producer blocks.
_PRODUCER_BACKLOG_SIZE = 64
# Backpressure threshold: push stream sleeps when buffered audio exceeds this.
_PRODUCER_BUFFER_LIMIT_US = 30_000_000
# Start join promotion once catchup processor lag is within this window of the history tail.
_JOIN_PROMOTE_ARM_WINDOW_US = 2_000_000
# Accept catchup output within this margin of the promotion target.
_JOIN_PROMOTE_TOLERANCE_US = 50_000
# Abort join catchup if promotion hasn't completed within this.
_JOIN_PROMOTION_TIMEOUT_S = 15.0
# Retain committed history this far behind real-time for late-join backfill.
# This pre-history also warms up ffmpeg's internal filter buffers so the DSP
# output has settled by the time the member's channel goes live.
_HISTORY_KEEP_PAST_US = 1_000_000


class _BufferedFfmpegProcessor:
    """FFmpeg wrapper with small output carry-over buffer and duration-based reads."""

    def __init__(self, ffmpeg: FFMpeg, audio_format: AudioFormat) -> None:
        self._ffmpeg = ffmpeg
        self._output_buffer = bytearray()
        bytes_per_sample = max(1, int(audio_format.bit_depth // 8))
        self._sample_rate = int(audio_format.sample_rate)
        self._frame_size = bytes_per_sample * int(audio_format.channels)
        self._bytes_per_second = self._sample_rate * self._frame_size
        # ~25ms worth of audio per read syscall.
        self._read_quantum_bytes = max(1, int(self._bytes_per_second * 0.025))
        self._produced_output_us = 0
        self._pending_skip_bytes = 0

    async def start(self) -> None:
        await self._ffmpeg.start()

    async def close(self) -> None:
        await self._ffmpeg.close()

    async def push(self, pcm: bytes) -> None:
        await self._ffmpeg.write(pcm)

    async def write_eof(self) -> None:
        """Signal no more input, causing ffmpeg to flush its internal buffers."""
        await self._ffmpeg.write_eof()

    @property
    def produced_output_us(self) -> int:
        """Return cumulative output duration currently drained from ffmpeg."""
        return self._produced_output_us

    async def read_duration_us(self, duration_us: int) -> bytes:
        """Block-read exactly `duration_us` worth of processed PCM from ffmpeg."""
        target_bytes = self._target_bytes_for_duration_us(duration_us)
        if target_bytes == 0:
            return b""

        while len(self._output_buffer) < target_bytes:
            missing = target_bytes - len(self._output_buffer)
            read_size = max(self._read_quantum_bytes, missing)
            chunk = await self._ffmpeg.readexactly(read_size)
            if not chunk:
                break
            chunk = self._consume_pending_skip(chunk)
            if chunk:
                self._output_buffer.extend(chunk)

        out = bytes(self._output_buffer[:target_bytes])
        del self._output_buffer[:target_bytes]
        return out

    async def drain_available(self) -> int:
        """
        Non-blocking drain of ffmpeg stdout into internal buffer.

        Returns cumulative produced output duration in microseconds.
        """
        while True:
            try:
                # 1ms timeout: non-blocking check for available data.
                chunk = await asyncio.wait_for(
                    self._ffmpeg.read(self._read_quantum_bytes),
                    timeout=0.001,
                )
            except TimeoutError:
                break
            if not chunk:
                break
            self._produced_output_us += self._duration_us_for_bytes(len(chunk))
            chunk = self._consume_pending_skip(chunk)
            if chunk:
                self._output_buffer.extend(chunk)
            if len(chunk) < self._read_quantum_bytes:
                break
        return self._produced_output_us

    async def drain_forever(self) -> None:
        """Continuously drain ffmpeg stdout into internal buffer until EOF."""
        while True:
            chunk = await self._ffmpeg.read(self._read_quantum_bytes)
            if not chunk:
                break
            self._produced_output_us += self._duration_us_for_bytes(len(chunk))
            chunk = self._consume_pending_skip(chunk)
            if chunk:
                self._output_buffer.extend(chunk)

    def pop_duration_us(self, duration_us: int) -> bytes | None:
        """Pop exactly `duration_us` from already buffered output, or None if insufficient."""
        target_bytes = self._target_bytes_for_duration_us(duration_us)
        if target_bytes == 0:
            return b""
        if len(self._output_buffer) < target_bytes:
            return None
        out = bytes(self._output_buffer[:target_bytes])
        del self._output_buffer[:target_bytes]
        return out

    def buffered_duration_us(self) -> int:
        """Return buffered output duration currently available for immediate pop."""
        return self._duration_us_for_bytes(len(self._output_buffer))

    def pop_duration_us_or_pad(self, duration_us: int, pad_tolerance_us: int) -> bytes | None:
        """Pop target duration; if short within tolerance, pad tail with silence."""
        target_bytes = self._target_bytes_for_duration_us(duration_us)
        if target_bytes == 0:
            return b""
        available = len(self._output_buffer)
        if available >= target_bytes:
            out = bytes(self._output_buffer[:target_bytes])
            del self._output_buffer[:target_bytes]
            return out
        short_bytes = target_bytes - available
        short_us = self._duration_us_for_bytes(short_bytes)
        if short_us > max(0, pad_tolerance_us):
            return None
        out = bytes(self._output_buffer)
        self._output_buffer.clear()
        self._pending_skip_bytes += short_bytes
        return out + (b"\x00" * short_bytes)

    def pad_and_skip(self, duration_us: int) -> bytes:
        """Return silence PCM and skip the equivalent from upcoming ffmpeg output."""
        target_bytes = self._target_bytes_for_duration_us(duration_us)
        if target_bytes <= 0:
            return b""
        # Drop buffered output with stale source positions.
        leftover = len(self._output_buffer)
        self._output_buffer.clear()
        self._pending_skip_bytes += max(0, target_bytes - leftover)
        return b"\x00" * target_bytes

    def _consume_pending_skip(self, chunk: bytes) -> bytes:
        # Drops bytes pop_duration_us_or_pad replaced with silence to keep timeline aligned.
        if self._pending_skip_bytes <= 0 or not chunk:
            return chunk
        skip = min(self._pending_skip_bytes, len(chunk))
        self._pending_skip_bytes -= skip
        return chunk[skip:]

    def _duration_us_for_bytes(self, byte_count: int) -> int:
        if byte_count <= 0 or self._sample_rate <= 0 or self._frame_size <= 0:
            return 0
        frames = byte_count // self._frame_size
        if frames <= 0:
            return 0
        return int((frames * 1_000_000) / self._sample_rate)

    def _target_bytes_for_duration_us(self, duration_us: int) -> int:
        """Convert duration to frame-aligned PCM byte count."""
        if duration_us <= 0 or self._sample_rate <= 0 or self._frame_size <= 0:
            return 0
        samples = max(0, int((duration_us * self._sample_rate + 500_000) / 1_000_000))
        return samples * self._frame_size


@dataclass(slots=True)
class _HistoryChunk:
    start_time_us: int
    duration_us: int
    pcm: bytes


@dataclass(slots=True)
class _PendingChunk:
    pcm: bytes
    duration_us: int


@dataclass(slots=True)
class _JoinCatchupState:
    """
    Per-member state for a join-catchup processor replaying history through DSP.

    The processor is fed historical + live PCM via ``input_queue``.  Once its
    output catches up to the live stream (within tolerance), it is promoted to
    the member's live pipeline.  See ``_inject_ready_join_historical`` for the
    full promotion lifecycle.
    """

    processor: _BufferedFfmpegProcessor
    input_queue: asyncio.Queue[bytes | None]
    writer_task: asyncio.Task[None]
    drainer_task: asyncio.Task[None]
    snapshot_task: asyncio.Task[None] | None = None
    # Timeline position of the first history chunk fed into the processor.
    first_history_start_us: int | None = None
    # Timeline position up to which PCM has been enqueued into the processor.
    fed_until_us: int | None = None
    # End of the history snapshot taken when catchup started.
    history_end_us: int | None = None
    # Locked target: once set, promotion fires when output reaches this point.
    promotion_target_end_us: int | None = None
    # Monotonic time when promotion was armed, used for timeout detection.
    promotion_armed_monotonic_s: float | None = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class _PipelineConfig:
    requires_transform: bool
    output_channels: str
    filter_params: tuple[str | ComplexFilter, ...]

    @property
    def signature(self) -> tuple[bool, str, tuple[str | ComplexFilter, ...]]:
        return (self.requires_transform, self.output_channels, self.filter_params)


@dataclass(slots=True)
class _MemberPipeline:
    player_id: str
    channel_id: UUID
    config: _PipelineConfig
    processor: _BufferedFfmpegProcessor | None = None
    ready: bool = False


class SendspinPlaybackSession:
    """
    Coordinates playback for a Sendspin player group leader.

    The push stream supports multi-channel audio: members that need per-player
    DSP (EQ, channel mixing, output routing) each get a dedicated ffmpeg
    processor and a separate channel. Members without DSP share MAIN_CHANNEL
    and receive the raw PCM directly.

    Playback runs as two concurrent coroutines inside ``_run_playback``:

    * **Producer** -- reads PCM from the MA stream, slices it into fixed-size
      chunks, queues them, and writes each slice into per-member ffmpeg
      processors (transform push) in parallel.
    * **Consumer** -- dequeues chunks, reads the corresponding transformed
      output from each processor (transform read), prepares all channels on
      the push stream, commits audio, and applies backpressure via
      ``sleep_to_limit_buffer``.

    When a new member joins mid-playback, a *join-catchup* processor replays
    committed history through the member's DSP chain so it can be promoted
    to the live pipeline without an audible gap.
    """

    def __init__(self, player: SendspinPlayer) -> None:
        """Initialize session coordinator bound to the owning player."""
        self.player = player
        self.playback_task: asyncio.Task[None] | None = None
        self.pending_join_members: set[str] = set()
        self._state_lock = asyncio.Lock()
        self._members: set[str] = set()
        self._member_pipelines: dict[str, _MemberPipeline] = {}
        self._push_stream: PushStream | None = None
        self._playback_running = False
        self._producer_eof_sent = False
        self._timeline_start_us: int | None = None
        self._first_commit_monotonic_us: int | None = None
        self._produced_audio_us = 0
        self._history: deque[_HistoryChunk] = deque()
        self._join_catchup: dict[str, _JoinCatchupState] = {}
        self._pipeline_config_cache: dict[str, _PipelineConfig] = {}
        self._preassigned_channels: dict[str, UUID] = {}
        self._mapping_dirty = True
        self._cancel_requested = False
        # PCM formats are session-scoped and refreshed at the start of every
        # _run_playback: the wire/MA-side rate is taken from the leader player's
        # preferred format (capped at 48 kHz for lossy codecs), F32 is always used
        # internally for DSP headroom.
        self._pcm_format: AudioFormat = _DEFAULT_PCM_FORMAT
        self._sendspin_pcm_format: SendspinAudioFormat = _DEFAULT_SENDSPIN_PCM_FORMAT
        self._queue_id: str | None = None
        self._queue_session_id: str | None = None

    def flow_track_anchor_us(self, track_start_offset_us: int) -> int | None:
        """
        Server-clock time of the current flow track's file-position 0.

        ``track_start_offset_us`` is the current track's start offset within the
        flow stream (minus its file seek), so beats timed from the track file
        map onto the shared audio timeline regardless of queue position. Returns
        None until the first chunk commits and the timeline anchor is known.
        """
        if self._timeline_start_us is None:
            return None
        return self._timeline_start_us + track_start_offset_us

    # -- Public API ------------------------------------------------------------

    async def transfer_to(self, new_player: SendspinPlayer) -> None:
        """
        Transfer session ownership to a new player.

        Used during dynamic leader switching to keep the push stream alive
        while the old leader is removed from the sendspin group. The PushStream
        and all internal state (pipelines, history, join-catchup) stay intact;
        only the owning player reference is updated.

        Cleans up the old leader's pipeline/channel state so its FFmpeg
        processor is released.

        :param new_player: The SendspinPlayer that will take over as session owner.
        """
        old_leader_id = self.player.player_id
        self.player = new_player
        # Release the old leader's DSP pipeline -- it's no longer in the group
        # and _refresh_member_mappings won't touch it since it only iterates
        # current members + the (new) leader.
        async with self._state_lock:
            pipeline = self._member_pipelines.pop(old_leader_id, None)
            self._pipeline_config_cache.pop(old_leader_id, None)
            self._preassigned_channels.pop(old_leader_id, None)
            self._mapping_dirty = True
        if pipeline is not None and pipeline.processor is not None:
            await self._close_member_ffmpeg(pipeline.processor)

    async def cancel(self, reason: str, *, keep_stream: bool = False) -> None:
        """
        Cancel and await the active playback task, if any.

        :param reason: Why the task is being cancelled, for logging and the cancel message.
        :param keep_stream: Keep the stream active for a track change and only have clients
            clear their buffers. Ignored while legacy clients are allowed, since they might
            mishandle stream/clear.
        """
        task = self.playback_task
        if task is None:
            return
        if task.done():
            if self.playback_task is task:
                self.playback_task = None
            return
        provider = cast("SendspinProvider", self.player.provider)
        if provider.server_api.allow_noncompliant_clients:
            keep_stream = False
        self.player.logger.debug("Cancelling playback task (%s)", reason)
        self._cancel_requested = True
        task.cancel(msg=reason)
        if keep_stream:
            with suppress(Exception):
                self._stop_push_stream(keep_stream=True)
        with suppress(asyncio.CancelledError, Exception):
            await task
        if self.playback_task is task:
            self.playback_task = None

    async def start(self, media: PlayerMedia, restart: bool = False) -> None:
        """Start background playback for `media`."""
        active_task = self.playback_task
        if active_task is not None and not active_task.done():
            if not restart:
                raise RuntimeError("playback already active")
            await self.cancel("restart requested", keep_stream=True)
        self._cancel_requested = False
        self.playback_task = asyncio.create_task(self._run_playback(media))
        self._attach_task_exception_logger(self.playback_task, "playback")

    async def close(self) -> None:
        """Stop playback and release all managed resources."""
        await self.cancel("session close")
        self.pending_join_members.clear()
        async with self._state_lock:
            self._members.clear()
            self._mapping_dirty = True
        await self._clear_member_pipelines()
        await self._clear_join_catchup()
        async with self._state_lock:
            self._history.clear()
            self._produced_audio_us = 0
            self._timeline_start_us = None
            self._first_commit_monotonic_us = None
            self._pipeline_config_cache.clear()
            self._preassigned_channels.clear()

    async def add_member(self, player_id: str) -> None:
        """Add a member to the group with DSP-aware lifecycle handling."""
        async with self._state_lock:
            if player_id in self._members:
                return
            self.pending_join_members.add(player_id)
            # Preserve any channel pre-resolved during add_client so join-time
            # role requirements and prepared audio stay on the same channel.
            self._preassigned_channels.setdefault(player_id, uuid4())
        try:
            await self._start_join_catchup(player_id)
        except Exception:
            async with self._state_lock:
                self.pending_join_members.discard(player_id)
            await self._release_player_channel(player_id)
            raise
        # Promote to full member even if already pending to avoid losing
        # the join when a cancelled task clears our pending flag.
        async with self._state_lock:
            if player_id not in self.pending_join_members:
                return
            self._members.add(player_id)
            self._mapping_dirty = True
            self.pending_join_members.discard(player_id)

    async def remove_member(self, player_id: str) -> None:
        """Remove a member from the group and clean up per-member playback state."""
        async with self._state_lock:
            self.pending_join_members.discard(player_id)
            self._members.discard(player_id)
            self._mapping_dirty = True
            self._pipeline_config_cache.pop(player_id, None)
            self._preassigned_channels.pop(player_id, None)
        await self._stop_join_catchup(player_id)
        await self._release_player_channel(player_id)

    async def sync_members(self, member_ids: set[str]) -> None:
        """Reconcile session members to exactly the provided set."""
        async with self._state_lock:
            current_members = set(self._members)
            stale_pending = self.pending_join_members - member_ids
        for player_id in stale_pending:
            await self.remove_member(player_id)
        for player_id in current_members - member_ids:
            await self.remove_member(player_id)
        for player_id in member_ids - current_members:
            await self.add_member(player_id)

    # -- Helpers ---------------------------------------------------------------

    def _attach_task_exception_logger(self, task: asyncio.Task[Any], name: str) -> None:
        """Log unhandled exception from background task when it finishes."""

        def _done_callback(done_task: asyncio.Task[Any]) -> None:
            if done_task.cancelled():
                return
            with suppress(Exception):
                exc = done_task.exception()
                if exc is not None:
                    self.player.logger.exception(
                        "Background task failed: %s",
                        name,
                        exc_info=exc,
                    )

        task.add_done_callback(_done_callback)

    def _get_join_readiness(self) -> tuple[bool, str | None]:
        """Check whether live join DSP preparation can be performed right now."""
        if self._playback_running and self._push_stream is not None:
            return (True, None)
        return (False, "no active stream context")

    # -- Snapshot helper -------------------------------------------------------

    async def _snapshot_active_pipelines(
        self,
    ) -> tuple[set[str], tuple[tuple[str, _MemberPipeline], ...]]:
        """Return (join_pending_ids, active_pipelines) under lock."""
        async with self._state_lock:
            members = self._members
            leader_id = self.player.player_id
            return set(self._join_catchup), tuple(
                (mid, p)
                for mid, p in self._member_pipelines.items()
                if mid in members or mid == leader_id
            )

    # -- Join catchup ----------------------------------------------------------

    async def _start_join_catchup(self, player_id: str) -> None:  # noqa: PLR0915
        """Start dedicated join catchup processor fed from committed history."""
        async with self._state_lock:
            playback_active = self._playback_running and self._push_stream is not None
        if not playback_active:
            return

        pipeline = await self._sync_member_pipeline(player_id)
        if not pipeline.config.requires_transform:
            return

        await self._stop_join_catchup(player_id)

        ffmpeg_obj = self._create_member_ffmpeg(pipeline.config.filter_params)
        processor = _BufferedFfmpegProcessor(ffmpeg_obj, self._pcm_format)
        await processor.start()
        # Bounded queue sized to hold the full buffer duration with some headroom.
        queue_size = (_PRODUCER_BUFFER_LIMIT_US // _PRODUCER_SLICE_US) + _PRODUCER_BACKLOG_SIZE
        input_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_size)

        async with self._state_lock:
            history_snapshot = list(self._history)
        if not history_snapshot:
            await processor.close()
            return
        history_end_us = history_snapshot[-1].start_time_us + history_snapshot[-1].duration_us
        writer_task: asyncio.Task[None] | None = None
        drainer_task: asyncio.Task[None] | None = None
        snapshot_task: asyncio.Task[None] | None = None
        state: _JoinCatchupState | None = None
        registered = False

        async def _writer() -> None:
            while True:
                chunk = await input_queue.get()
                if chunk is None:
                    return
                await processor.push(chunk)

        async def _drainer() -> None:
            await processor.drain_forever()

        try:
            async with self._state_lock:
                writer_task = asyncio.create_task(_writer())
                drainer_task = asyncio.create_task(_drainer())
                self._attach_task_exception_logger(writer_task, f"join_writer_{player_id}")
                self._attach_task_exception_logger(drainer_task, f"join_drainer_{player_id}")

                state = _JoinCatchupState(
                    processor=processor,
                    input_queue=input_queue,
                    writer_task=writer_task,
                    drainer_task=drainer_task,
                    history_end_us=history_end_us,
                )
                self._join_catchup[player_id] = state
                snapshot_task = asyncio.create_task(
                    self._feed_join_history(player_id, processor, history_snapshot)
                )
                self._attach_task_exception_logger(snapshot_task, f"join_snapshot_{player_id}")
                state.snapshot_task = snapshot_task
                registered = True
        except BaseException:
            if not registered:
                # If registered, cleanup is handled via _stop_join_catchup/_clear_join_catchup.
                async with self._state_lock:
                    current = self._join_catchup.get(player_id)
                    if current is state:
                        self._join_catchup.pop(player_id, None)
                for task in (snapshot_task, drainer_task, writer_task):
                    if task is None:
                        continue
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
                with suppress(Exception):
                    await processor.close()
            raise

    async def _feed_join_history(
        self,
        player_id: str,
        processor: _BufferedFfmpegProcessor,
        history_snapshot: list[_HistoryChunk],
    ) -> None:
        """Feed historical PCM into a join-catchup processor."""
        async with self._state_lock:
            state = self._join_catchup.get(player_id)
        if state is None or state.processor is not processor:
            return
        async with state.write_lock:
            first_history_start_us: int | None = None
            previous_end_us: int | None = None
            for hist_chunk in history_snapshot:
                if first_history_start_us is None:
                    first_history_start_us = hist_chunk.start_time_us
                    async with self._state_lock:
                        current = self._join_catchup.get(player_id)
                        if current is not None and current.processor is processor:
                            current.first_history_start_us = first_history_start_us
                            current.fed_until_us = first_history_start_us
                if previous_end_us is not None and hist_chunk.start_time_us > previous_end_us:
                    gap_us = hist_chunk.start_time_us - previous_end_us
                    silence = self._silence_for_duration_us(gap_us)
                    if silence:
                        await self._enqueue_join_pcm(state, silence)
                await self._enqueue_join_pcm(state, hist_chunk.pcm)
                previous_end_us = hist_chunk.start_time_us + hist_chunk.duration_us
                async with self._state_lock:
                    current = self._join_catchup.get(player_id)
                    if current is not None and current.processor is processor:
                        current.fed_until_us = previous_end_us

    async def _stop_join_catchup(self, player_id: str) -> None:
        """Stop and remove dedicated join catchup processor for one player."""
        async with self._state_lock:
            state = self._join_catchup.pop(player_id, None)
        if state is None:
            return
        if state.snapshot_task is not None:
            state.snapshot_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await state.snapshot_task
        state.writer_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.writer_task
        state.drainer_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.drainer_task
        with suppress(Exception):
            await state.processor.close()

    async def _promote_join_catchup_processor(
        self,
        player_id: str,
        pipeline: _MemberPipeline,
        target_end_us: int,
    ) -> None:
        """Promote join catchup processor to the member's live DSP processor."""
        old_processor: _BufferedFfmpegProcessor | None = None
        async with self._state_lock:
            state = self._join_catchup.pop(player_id, None)
            if state is None:
                return
            old_processor = pipeline.processor
            pipeline.processor = state.processor
        if state.snapshot_task is not None:
            state.snapshot_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await state.snapshot_task
        # Let writer flush queued PCM before handoff; cancel if queue is full.
        try:
            state.input_queue.put_nowait(None)
        except asyncio.QueueFull:
            state.writer_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.writer_task
        state.drainer_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await state.drainer_task
        if self._producer_eof_sent and pipeline.config.requires_transform:
            with suppress(Exception):
                await pipeline.processor.write_eof()
        if old_processor is not None and old_processor is not state.processor:
            with suppress(Exception):
                await old_processor.close()

    async def _clear_join_catchup(self) -> None:
        """Stop and remove all dedicated join catchup processors."""
        async with self._state_lock:
            player_ids = list(self._join_catchup.keys())
        for player_id in player_ids:
            await self._stop_join_catchup(player_id)

    async def _release_player_channel(self, player_id: str) -> None:
        """Release per-member channel/DSP state for a removed member."""
        async with self._state_lock:
            pipeline = self._member_pipelines.pop(player_id, None)
            self._preassigned_channels.pop(player_id, None)
        if pipeline is None or pipeline.processor is None:
            return
        await self._close_member_ffmpeg(pipeline.processor)

    # -- Playback pipeline -----------------------------------------------------

    async def _run_playback(self, media: PlayerMedia) -> None:  # noqa: PLR0915
        """
        Run the playback pipeline for a single media session.

        Pulls PCM from the MA stream, feeds main + per-member DSP channels into the
        Sendspin push stream, and commits audio continuously. Supports dynamic group
        membership changes and late-join historical backfill while running.
        """
        # aiosendspin resamples and encodes with PyAV, which it imports lazily on first use -
        # from inside commit_audio(), on the event loop. Pull that import forward to a thread,
        # before the play timeline exists, so its cost can neither stall audio production nor
        # push the timeline into a forward rebase.
        await import_module_in_thread("av")
        push_stream: PushStream | None = None
        try:
            # refresh the session PCM format from the leader's preferred output before
            # building any pipelines; member ffmpeg pipelines and pre-computed filter
            # params depend on this rate so the cache must also be cleared
            self._pcm_format, self._sendspin_pcm_format = self._select_session_pcm_formats()
            self._queue_id = media.source_id
            self._queue_session_id = get_media_session_id(media)
            self._pipeline_config_cache.clear()
            self.player.logger.debug(
                "Sendspin session PCM format: %d Hz / F32",
                self._pcm_format.sample_rate,
            )
            push_stream = self._create_push_stream()
            push_stream.set_live_source(self._is_live_source(media))
            async with self._state_lock:
                self._push_stream = push_stream
                self._playback_running = True
                self._producer_eof_sent = False
                self._history.clear()
                self._produced_audio_us = 0
                self._timeline_start_us = None
                self._first_commit_monotonic_us = None
                self._mapping_dirty = True
        except Exception:
            # A track change stops the previous stream without stream/end, so a failed
            # setup has to end this one. Cancellation propagates untouched, since there
            # the successor keeps the stream.
            if push_stream is not None:
                with suppress(Exception):
                    push_stream.stop()
            await self._reset_session_state()
            raise
        # Bounded queue between producer (stream reader) and consumer (committer).
        pending_chunks: asyncio.Queue[_PendingChunk | None] = asyncio.Queue(
            maxsize=_PRODUCER_BACKLOG_SIZE
        )
        # Shadow deque mirroring pending_chunks for join-catchup backlog peeking.
        pending_backlog: deque[_PendingChunk] = deque()
        pending_duration_us = 0
        last_elapsed_update_s = 0.0

        async def _produce_pending_chunks() -> None:
            nonlocal pending_duration_us
            audio_source = self.player.mass.streams.get_stream(
                media, self._pcm_format, self.player.player_id
            )
            completed = False
            try:
                async for chunk in audio_source:
                    if not chunk:
                        continue
                    for slice_chunk in iter_pcm_slices(
                        chunk, self._pcm_format, target_duration_ms=_PRODUCER_SLICE_US // 1000
                    ):
                        if not slice_chunk:
                            continue
                        duration_us = self._duration_us(slice_chunk, self._pcm_format)
                        if duration_us <= 0:
                            continue
                        await self._refresh_member_mappings()
                        pending = _PendingChunk(pcm=slice_chunk, duration_us=duration_us)
                        await pending_chunks.put(pending)
                        pending_backlog.append(pending)
                        pending_duration_us += duration_us
                        join_pending_ids, pipelines = await self._snapshot_active_pipelines()
                        transform_pipelines: list[_MemberPipeline] = []
                        for member_id, pipeline in pipelines:
                            if not pipeline.config.requires_transform:
                                continue
                            if member_id in join_pending_ids:
                                continue
                            transform_pipelines.append(pipeline)
                        results = await asyncio.gather(
                            *(
                                self._transform_member_chunk(pipeline, slice_chunk)
                                for pipeline in transform_pipelines
                            ),
                            return_exceptions=True,
                        )
                        for pipeline, result in zip(transform_pipelines, results, strict=True):
                            if isinstance(result, BaseException):
                                self.player.logger.warning(
                                    "Transform push failed for channel %s: %s",
                                    pipeline.channel_id,
                                    result,
                                )
                completed = True
            finally:
                if not completed:
                    close_task = asyncio.create_task(audio_source.aclose())
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError:
                        await close_task
                        raise

        async def _commit_pending_chunks() -> None:
            nonlocal pending_duration_us, last_elapsed_update_s
            while True:
                pending = await pending_chunks.get()
                if pending is None:
                    break
                pending_backlog.popleft()
                pending_duration_us = max(0, pending_duration_us - pending.duration_us)
                await self._inject_ready_join_historical(push_stream, pending_backlog, pending.pcm)
                push_stream.prepare_audio(
                    pending.pcm, self._sendspin_pcm_format, channel_id=MAIN_CHANNEL
                )
                join_pending_ids, pipelines = await self._snapshot_active_pipelines()
                transform_pipelines: list[_MemberPipeline] = []
                for member_id, pipeline in pipelines:
                    if not pipeline.config.requires_transform:
                        continue
                    if member_id in join_pending_ids:
                        continue
                    transform_pipelines.append(pipeline)
                transformed_chunks = await asyncio.gather(
                    *(
                        self._read_member_chunk(pipeline, pending.duration_us)
                        for pipeline in transform_pipelines
                    ),
                    return_exceptions=True,
                )
                for pipeline, transformed_chunk in zip(
                    transform_pipelines, transformed_chunks, strict=True
                ):
                    if isinstance(transformed_chunk, BaseException):
                        self.player.logger.warning(
                            "Transform read failed for channel %s: %s",
                            pipeline.channel_id,
                            transformed_chunk,
                        )
                        continue
                    if transformed_chunk is None:
                        continue
                    push_stream.prepare_audio(
                        transformed_chunk,
                        self._sendspin_pcm_format,
                        channel_id=pipeline.channel_id,
                    )
                try:
                    commit_start_us = await push_stream.commit_audio()
                except StreamStoppedError:
                    # Stream stopped since it was replaced by another stream
                    self.player.logger.debug("Stopping commit loop due to stopped push stream")
                    break
                await push_stream.sleep_to_limit_buffer(_PRODUCER_BUFFER_LIMIT_US)
                commit_now_us = push_stream.now_us()
                committed_history_chunk = _HistoryChunk(
                    start_time_us=int(commit_start_us),
                    duration_us=pending.duration_us,
                    pcm=pending.pcm,
                )
                async with self._state_lock:
                    if self._timeline_start_us is None:
                        self._timeline_start_us = int(commit_start_us)
                    if self._first_commit_monotonic_us is None:
                        self._first_commit_monotonic_us = commit_now_us
                    self._history.append(committed_history_chunk)
                    self._produced_audio_us += pending.duration_us
                    self._prune_history_locked(commit_now_us)
                await self._fanout_history_chunk_to_join_processors(committed_history_chunk)
                if self._timeline_start_us is not None:
                    elapsed_real_s = max(0.0, (commit_now_us - self._timeline_start_us) / 1_000_000)
                    if elapsed_real_s - last_elapsed_update_s >= 1.0:
                        last_elapsed_update_s = elapsed_real_s
                        self.player._attr_elapsed_time = elapsed_real_s
                        self.player._attr_elapsed_time_last_updated = time.time()
                        self.player.update_state()

        commit_task = asyncio.create_task(_commit_pending_chunks())
        self._attach_task_exception_logger(commit_task, "commit_pending_chunks")
        producer_stopped_cleanly = False
        try:
            await _produce_pending_chunks()
            producer_stopped_cleanly = True
        finally:
            if producer_stopped_cleanly and not self._cancel_requested and not commit_task.done():
                # Mark EOF so that catchup processors promoted after this
                # point also get flushed (see _promote_join_catchup_processor).
                self._producer_eof_sent = True
                # Signal EOF to transform pipelines so ffmpeg flushes its
                # internal buffers instead of blocking on the last read.
                _, pipelines = await self._snapshot_active_pipelines()
                for _, pipeline in pipelines:
                    if pipeline.processor is not None and pipeline.config.requires_transform:
                        with suppress(Exception):
                            await pipeline.processor.write_eof()
                # Producer finished normally; send a None sentinel so the
                # consumer exits cleanly.  The queue may be full, so retry
                # with a deadline before falling back to cancellation.
                sentinel_sent = False
                deadline = time.monotonic() + 1.0
                while not sentinel_sent and not commit_task.done():
                    try:
                        pending_chunks.put_nowait(None)
                        sentinel_sent = True
                    except asyncio.QueueFull:
                        if time.monotonic() >= deadline:
                            break
                        await asyncio.sleep(0.01)
                if not sentinel_sent:
                    commit_task.cancel()
            else:
                commit_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await commit_task
            # On clean EOF, wait for clients to finish playing their
            # buffered audio before sending stream/end (which clears
            # client buffers per the Sendspin spec). Skip this when a
            # new playback is superseding this one, so skipping tracks is still fast.
            if producer_stopped_cleanly and not self._cancel_requested:
                try:
                    await self._wait_for_buffer_drain()
                except asyncio.CancelledError:
                    # New playback interrupted the drain — treat as
                    # non-clean stop so we skip group.stop() below
                    # and let the new playback handle the transition.
                    producer_stopped_cleanly = False
            with suppress(Exception):
                # Same condition as the group.stop() below, so we snapshot on exactly the
                # paths where a group STOP - and therefore a freeze - is already emitted.
                self._stop_push_stream(
                    snapshot_progress=producer_stopped_cleanly and not self._cancel_requested,
                )
            await self._clear_join_catchup()
            await self._clear_member_pipelines()
            await self._reset_session_state()
            # Only emit a group STOP when MA stream playback reached natural EOF.
            # Skip this on cancellation/error paths to avoid stop-event races with transitions.
            if producer_stopped_cleanly and not self._cancel_requested:
                with suppress(Exception):
                    await self.player.api.group.stop()

    # -- Join injection --------------------------------------------------------

    async def _inject_ready_join_historical(
        self,
        push_stream: PushStream,
        pending_backlog: deque[_PendingChunk],
        current_pcm: bytes,
    ) -> bool:
        """
        Inject join-catchup historical audio once processor output reaches history end.

        Join promotion lifecycle:
        1. A catchup processor is fed historical PCM and new commits in parallel.
        2. Once the processor's output lag falls within _JOIN_PROMOTE_ARM_WINDOW_US
           of the history tail, promotion is "armed" and a target end timestamp is locked.
        3. Once output reaches the target (within _JOIN_PROMOTE_TOLERANCE_US), the
           catchup processor is promoted to the member's live DSP pipeline.
        4. If promotion doesn't complete within _JOIN_PROMOTION_TIMEOUT_S, it's aborted.
        """
        injected_any = False
        async with self._state_lock:
            items = list(self._join_catchup.items())
        for player_id, state in items:
            produced_output_us = state.processor.produced_output_us
            async with self._state_lock:
                current = self._join_catchup.get(player_id)
                if current is None or current.processor is not state.processor:
                    continue
                first_history_start_us = current.first_history_start_us
                fed_until_us = current.fed_until_us
                history_end_us = current.history_end_us
                promotion_target_end_us = current.promotion_target_end_us
                promotion_armed_monotonic_s = current.promotion_armed_monotonic_s
            if first_history_start_us is None or fed_until_us is None or history_end_us is None:
                continue
            max_ready_end_us = min(
                fed_until_us,
                first_history_start_us + max(0, produced_output_us),
            )
            if promotion_target_end_us is None:
                lag_to_tail_us = history_end_us - max_ready_end_us
                if lag_to_tail_us > _JOIN_PROMOTE_ARM_WINDOW_US:
                    continue
                async with self._state_lock:
                    current = self._join_catchup.get(player_id)
                    if current is None or current.processor is not state.processor:
                        continue
                    if current.promotion_target_end_us is None:
                        current.promotion_target_end_us = history_end_us
                        current.promotion_armed_monotonic_s = time.monotonic()
                    promotion_target_end_us = current.promotion_target_end_us
                    promotion_armed_monotonic_s = current.promotion_armed_monotonic_s
            target_end_us = promotion_target_end_us
            if (
                promotion_armed_monotonic_s is not None
                and time.monotonic() - promotion_armed_monotonic_s > _JOIN_PROMOTION_TIMEOUT_S
            ):
                self.player.logger.error(
                    "Join promotion timed out for %s after %.1fs; dropping join catchup",
                    player_id,
                    _JOIN_PROMOTION_TIMEOUT_S,
                )
                await self._stop_join_catchup(player_id)
                continue
            if max_ready_end_us + _JOIN_PROMOTE_TOLERANCE_US < target_end_us:
                continue
            inject_duration_us = target_end_us - first_history_start_us
            transformed_history = state.processor.pop_duration_us_or_pad(
                inject_duration_us, _JOIN_PROMOTE_TOLERANCE_US
            )
            if transformed_history is None:
                continue
            transformed_history = await self._pad_history_to_live_tail(
                state, target_end_us, transformed_history
            )
            pipeline = await self._sync_member_pipeline(player_id)
            # Split the blob into slices so push_stream can yield between encodes.
            frame_stride = (
                self._sendspin_pcm_format.bit_depth // 8
            ) * self._sendspin_pcm_format.channels
            slice_bytes = (
                int(self._sendspin_pcm_format.sample_rate * _PRODUCER_SLICE_US / 1_000_000)
                * frame_stride
            )
            for offset in range(0, len(transformed_history), slice_bytes):
                push_stream.prepare_historical_audio(
                    transformed_history[offset : offset + slice_bytes],
                    self._sendspin_pcm_format,
                    channel_id=pipeline.channel_id,
                    start_time_us=first_history_start_us if offset == 0 else None,
                )
            await self._prefeed_pending_backlog_for_join(state, current_pcm, pending_backlog)
            await self._promote_join_catchup_processor(player_id, pipeline, target_end_us)
            injected_any = True
        return injected_any

    async def _pad_history_to_live_tail(
        self,
        state: _JoinCatchupState,
        target_end_us: int,
        transformed_history: bytes,
    ) -> bytes:
        """Append silence so joiner's channel_timing aligns with the live tail at promotion."""
        # target_end_us is locked from earlier and may lag the live tail by seconds.
        async with self._state_lock:
            live_tail_us = (
                self._history[-1].start_time_us + self._history[-1].duration_us
                if self._history
                else target_end_us
            )
        promotion_lag_us = max(0, live_tail_us - target_end_us)
        if promotion_lag_us <= 0:
            return transformed_history
        return transformed_history + state.processor.pad_and_skip(promotion_lag_us)

    async def _prefeed_pending_backlog_for_join(
        self,
        state: _JoinCatchupState,
        current_pcm: bytes,
        pending_backlog: deque[_PendingChunk],
    ) -> None:
        """
        Push current chunk + queued pending chunks into join processor before promotion.

        Between the last committed chunk and the next commit, there may be
        chunks already queued by the producer that the catchup processor hasn't
        seen yet.  Feeding them now avoids a gap in transformed audio after
        promotion.
        """
        await self._enqueue_join_pcm(state, current_pcm)
        for item in list(pending_backlog):
            await self._enqueue_join_pcm(state, item.pcm)

    async def _fanout_history_chunk_to_join_processors(self, hist_chunk: _HistoryChunk) -> None:
        """Feed newly committed history chunk into all active join-catchup processors."""
        async with self._state_lock:
            items = list(self._join_catchup.items())
        for player_id, state in items:
            async with state.write_lock:
                # Read current state under lock.
                async with self._state_lock:
                    current = self._join_catchup.get(player_id)
                    if current is None or current.processor is not state.processor:
                        continue
                    previous_end_us = current.fed_until_us
                    first_history_start_us = current.first_history_start_us
                # Initialize first_history_start_us if this is the first chunk.
                if first_history_start_us is None:
                    first_history_start_us = hist_chunk.start_time_us
                    previous_end_us = first_history_start_us
                # Fill timeline gaps with silence.
                if previous_end_us is not None and hist_chunk.start_time_us > previous_end_us:
                    gap_us = hist_chunk.start_time_us - previous_end_us
                    silence = self._silence_for_duration_us(gap_us)
                    if silence:
                        await self._enqueue_join_pcm(state, silence)
                await self._enqueue_join_pcm(state, hist_chunk.pcm)
                # Write updated state back under lock.
                new_end_us = hist_chunk.start_time_us + hist_chunk.duration_us
                async with self._state_lock:
                    current = self._join_catchup.get(player_id)
                    if current is not None and current.processor is state.processor:
                        if current.first_history_start_us is None:
                            current.first_history_start_us = first_history_start_us
                        if current.fed_until_us is None:
                            current.fed_until_us = first_history_start_us
                        current.fed_until_us = new_end_us
                        current.history_end_us = new_end_us

    async def _enqueue_join_pcm(
        self,
        state: _JoinCatchupState,
        pcm: bytes,
    ) -> None:
        """
        Enqueue PCM into a joining member writer queue.

        Bails out immediately if the writer task is dead to avoid blocking
        the commit loop on a queue with no consumer.
        """
        if state.writer_task.done():
            return
        try:
            state.input_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            if state.writer_task.done():
                return
            await state.input_queue.put(pcm)

    # -- Member pipeline management --------------------------------------------

    async def _refresh_member_mappings(self) -> None:
        """Re-evaluate per-member channel mapping and DSP requirements."""
        async with self._state_lock:
            if not self._mapping_dirty:
                return
            member_ids = tuple(self._members)
            self._mapping_dirty = False
        for member_id in member_ids:
            await self._sync_member_pipeline(member_id)
        # Keep leader pipeline in sync so leader DSP can be applied when required.
        await self._sync_member_pipeline(self.player.player_id)

    async def _sync_member_pipeline(self, player_id: str) -> _MemberPipeline:
        """Create/update pipeline state for one member from current MA config."""
        config = self._get_pipeline_config_cached(player_id)
        release_processor: _BufferedFfmpegProcessor | None = None
        start_processor: _BufferedFfmpegProcessor | None = None
        async with self._state_lock:
            current = self._member_pipelines.get(player_id)
            if current is not None and current.config.signature == config.signature:
                return current
            if current and current.config.requires_transform:
                channel_id = current.channel_id if config.requires_transform else MAIN_CHANNEL
                release_processor = current.processor
            elif config.requires_transform:
                channel_id = self._get_or_create_preassigned_channel(player_id)
            else:
                channel_id = MAIN_CHANNEL
                self._preassigned_channels.pop(player_id, None)
            processor: _BufferedFfmpegProcessor | None = None
            if config.requires_transform:
                ffmpeg_obj = self._create_member_ffmpeg(config.filter_params)
                processor = _BufferedFfmpegProcessor(ffmpeg_obj, self._pcm_format)
                start_processor = processor
            pipeline = _MemberPipeline(
                player_id=player_id,
                channel_id=channel_id,
                config=config,
                processor=processor,
            )
            self._member_pipelines[player_id] = pipeline
        if start_processor is not None:
            try:
                await start_processor.start()
            except Exception as err:
                async with self._state_lock:
                    if (
                        self._member_pipelines.get(player_id) is not None
                        and self._member_pipelines[player_id].processor is start_processor
                    ):
                        self._member_pipelines.pop(player_id, None)
                with suppress(Exception):
                    await self._close_member_ffmpeg(start_processor)
                raise RuntimeError(f"Failed to start member DSP ffmpeg for {player_id}") from err
        if release_processor is not None:
            await self._close_member_ffmpeg(release_processor)
        return pipeline

    def _get_pipeline_config_cached(
        self,
        player_id: str,
        *,
        force_refresh: bool = False,
    ) -> _PipelineConfig:
        """Return cached pipeline config for a player, calculating on cache miss."""
        if not force_refresh and (cached := self._pipeline_config_cache.get(player_id)) is not None:
            return cached
        config = self._read_pipeline_config(player_id)
        self._pipeline_config_cache[player_id] = config
        return config

    def _read_pipeline_config(self, player_id: str) -> _PipelineConfig:
        """Read MA config and determine if member needs a dedicated DSP channel."""
        dsp_config = self.player.mass.config.get_player_dsp_config(player_id)
        dsp_enabled = bool(dsp_config.enabled)
        raw_output_channels = self.player.mass.config.get_raw_player_config_value(
            player_id,
            CONF_OUTPUT_CHANNELS,
            "stereo",
        )
        output_channels = str(raw_output_channels or "stereo").strip().lower()
        if output_channels not in {"stereo", "left", "right", "mono"}:
            output_channels = "stereo"
        try:
            output_format = self._get_member_output_format(player_id)
            output_plan = self.player.mass.streams.audio.get_player_output_plan(
                player_id,
                self._pcm_format,
                output_format,
                handoff_format=self._pcm_format,
            )
            filter_params = tuple(output_plan.filter_params)
        except Exception:
            filter_params = ()
            output_plan = None
        # a ComplexFilter (e.g. convolution) is never a plain string, so it always counts
        custom_filter_graph = any(
            not isinstance(param, str) or param.strip() for param in filter_params
        )
        requires_transform = dsp_enabled or output_channels != "stereo" or custom_filter_graph
        if (
            output_plan is not None
            and self._queue_id is not None
            and self._queue_session_id is not None
        ):
            self.player.mass.streams.audio_processing.update_output(
                output_plan.output_details.player_ids[0],
                output_plan,
                queue_id=self._queue_id,
                session_id=self._queue_session_id,
            )
        return _PipelineConfig(
            requires_transform=requires_transform,
            output_channels=output_channels,
            filter_params=filter_params,
        )

    def _select_session_pcm_formats(self) -> tuple[AudioFormat, SendspinAudioFormat]:
        """
        Pick the session PCM format (MA-side + wire) from the leader's preferred format.

        F32 is always used for DSP headroom. The sample rate follows the leader's
        preferred rate but is capped at 48 kHz when the leader's output codec is
        lossy — higher rates yield no perceivable quality gain there. Member
        clients with a different preferred rate up/down-sample in their own DSP
        step on the receiving side.
        """
        leader_output = self._get_member_output_format(self.player.player_id)
        sample_rate = int(leader_output.sample_rate) or _DEFAULT_PCM_FORMAT.sample_rate
        if leader_output.content_type in (ContentType.OPUS, ContentType.MP3, ContentType.AAC):
            sample_rate = min(sample_rate, _LOSSY_MAX_SAMPLE_RATE)
        pcm_format = AudioFormat(
            content_type=ContentType.PCM_F32LE,
            sample_rate=sample_rate,
            bit_depth=32,
            channels=2,
        )
        sendspin_pcm_format = SendspinAudioFormat(
            sample_rate=sample_rate,
            bit_depth=32,
            channels=2,
            sample_type="float",
        )
        return pcm_format, sendspin_pcm_format

    def _get_member_output_format(self, player_id: str) -> AudioFormat:
        """
        Return the actual output AudioFormat for a group member.

        Derives the format from the member's sendspin player role (preferred codec
        and format), falling back to the internal PCM format if unavailable.
        """
        provider = cast("SendspinProvider", self.player.provider)
        client = provider.server_api.get_client(player_id)
        if client is not None:
            for role in client.roles_by_family("player"):
                if isinstance(role, PlayerV1Role):
                    preferred_fmt = role.preferred_format
                    preferred_codec = role.preferred_codec
                    if preferred_fmt is not None and preferred_codec is not None:
                        if preferred_codec == SendspinAudioCodec.FLAC:
                            content_type = ContentType.FLAC
                        elif preferred_codec == SendspinAudioCodec.OPUS:
                            content_type = ContentType.OPUS
                        else:
                            content_type = ContentType.from_bit_depth(preferred_fmt.bit_depth)
                        return AudioFormat(
                            content_type=content_type,
                            sample_rate=preferred_fmt.sample_rate,
                            bit_depth=preferred_fmt.bit_depth,
                            channels=preferred_fmt.channels,
                        )
                elif isinstance(role, BridgePlayerRole):
                    fmt = role.preferred_format
                    if fmt is not None:
                        return AudioFormat(
                            content_type=ContentType.from_bit_depth(fmt.bit_depth),
                            sample_rate=fmt.sample_rate,
                            bit_depth=fmt.bit_depth,
                            channels=fmt.channels,
                        )
                    return AudioFormat(
                        content_type=ContentType.from_bit_depth(BRIDGE_BIT_DEPTH),
                        sample_rate=BRIDGE_SAMPLE_RATE,
                        bit_depth=BRIDGE_BIT_DEPTH,
                        channels=BRIDGE_CHANNELS,
                    )
        return self._pcm_format

    def _get_or_create_preassigned_channel(self, player_id: str) -> UUID:
        """Return stable dedicated channel id for transform-required player."""
        if (channel_id := self._preassigned_channels.get(player_id)) is not None:
            return channel_id
        channel_id = uuid4()
        self._preassigned_channels[player_id] = channel_id
        return channel_id

    # -- FFmpeg lifecycle ------------------------------------------------------

    def _create_member_ffmpeg(self, filter_params: tuple[str | ComplexFilter, ...]) -> FFMpeg:
        """Create per-member FFMpeg for DSP pipeline."""
        return FFMpeg(
            audio_input="-",
            input_format=self._pcm_format,
            output_format=self._pcm_format,
            filter_params=list(filter_params),
        )

    async def _transform_member_chunk(self, pipeline: _MemberPipeline, chunk: bytes) -> None:
        """Push one PCM chunk into a member DSP pipeline."""
        processor = pipeline.processor
        if processor is None:
            return
        await processor.push(chunk)

    async def _read_member_chunk(
        self,
        pipeline: _MemberPipeline,
        duration_us: int,
    ) -> bytes | None:
        """Read one transformed chunk from a member DSP pipeline."""
        processor = pipeline.processor
        if processor is None or duration_us <= 0:
            return b""
        transformed = await processor.read_duration_us(duration_us)
        if not transformed:
            return None
        pipeline.ready = True
        return bytes(transformed)

    async def _close_member_ffmpeg(self, processor: _BufferedFfmpegProcessor) -> None:
        """Close an ffmpeg processor, suppressing errors."""
        with suppress(Exception):
            await processor.close()

    async def _clear_member_pipelines(self) -> None:
        """Release all member pipeline resources."""
        async with self._state_lock:
            pipelines = list(self._member_pipelines.values())
            self._member_pipelines.clear()
        for pipeline in pipelines:
            if pipeline.processor is not None:
                await self._close_member_ffmpeg(pipeline.processor)

    # -- Push stream -----------------------------------------------------------

    def _create_push_stream(self) -> PushStream:
        """Create PushStream with channel resolver for per-member routing."""
        return self.player.api.group.start_stream(channel_resolver=self._resolve_channel_for_player)

    async def _wait_for_buffer_drain(self) -> None:
        """
        Wait for clients to finish playing buffered audio.

        Called before stopping the push stream on natural EOF to prevent
        stream/end from clearing client buffers while audio is still playing.

        Uses the push stream's public backpressure API with a zero buffer
        target to sleep until the clock catches up with all committed audio.
        Each internal sleep is capped at 1 second, so we loop until drained.

        Raises asyncio.CancelledError if a new playback request interrupts.
        """
        ps = self._push_stream
        if ps is None or ps.is_stopped:
            return
        self.player.logger.debug("Waiting for client buffer drain before stream/end")
        # Safety timeout: never wait longer than the max buffer depth.
        deadline = time.monotonic() + (_PRODUCER_BUFFER_LIMIT_US / 1_000_000)
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            await ps.sleep_to_limit_buffer(0)
            # sleep_to_limit_buffer returns immediately when the clock has
            # caught up with all committed audio (nothing left to drain).
            if time.monotonic() - t0 < 0.05:
                break
        self.player.logger.debug("Client buffer drain complete")

    def _stop_push_stream(
        self, *, snapshot_progress: bool = False, keep_stream: bool = False
    ) -> None:
        """
        Stop the active PushStream.

        :param snapshot_progress: Freeze the group's playback progress first. Pass this
            only for a natural end of stream, never for one being superseded.
        :param keep_stream: Clear buffered audio without ending the client stream.
        """
        ps = self._push_stream
        if ps is None or ps.is_stopped:
            return
        if snapshot_progress and (metadata_role := self.player._metadata_role) is not None:
            # The group can only resolve the live position while the stream is up; once
            # it is down the freeze can just re-emit the last anchor that was pushed.
            metadata_role.freeze_progress()
        if keep_stream:
            ps.clear()
        ps.stop(keep_stream=keep_stream)

    async def _reset_session_state(self) -> None:
        """Drop all per-session playback state so the next session starts clean."""
        async with self._state_lock:
            self._push_stream = None
            self._playback_running = False
            self._timeline_start_us = None
            self._first_commit_monotonic_us = None
            self._produced_audio_us = 0
            self._history.clear()
            # Drop cached DSP decisions so next playback reflects latest config.
            self._pipeline_config_cache.clear()

    def _resolve_channel_for_player(self, player_id: str) -> UUID:
        """Channel resolver callback for per-player routing."""
        pipeline = self._member_pipelines.get(player_id)
        if pipeline is not None:
            return pipeline.channel_id
        # Force a fresh config read for pending/unknown joiners so the very
        # first resolution (triggered by add_client) uses up-to-date DSP settings.
        force = player_id not in self._members and player_id != self.player.player_id
        config = self._get_pipeline_config_cached(player_id, force_refresh=force)
        if not config.requires_transform:
            return MAIN_CHANNEL
        return self._get_or_create_preassigned_channel(player_id)

    # -- History ---------------------------------------------------------------

    def _prune_history_locked(self, now_monotonic_us: int) -> None:
        """Drop old history chunks that are fully in the past."""
        if self._timeline_start_us is None or self._first_commit_monotonic_us is None:
            return
        elapsed_real_us = max(0, now_monotonic_us - self._first_commit_monotonic_us)
        source_now_us = self._timeline_start_us + elapsed_real_us
        cutoff_us = source_now_us - _HISTORY_KEEP_PAST_US
        while self._history and (
            self._history[0].start_time_us + self._history[0].duration_us <= cutoff_us
        ):
            self._history.popleft()

    # -- PCM utilities ---------------------------------------------------------

    @staticmethod
    def _duration_us(audio: bytes, audio_format: AudioFormat) -> int:
        """Compute chunk duration from PCM payload size."""
        bytes_per_sample = max(1, int(audio_format.bit_depth // 8))
        bytes_per_second = (
            int(audio_format.sample_rate) * bytes_per_sample * int(audio_format.channels)
        )
        if bytes_per_second <= 0:
            return 0
        return int((len(audio) / bytes_per_second) * 1_000_000)

    def _silence_for_duration_us(self, duration_us: int) -> bytes:
        """Generate silent PCM with frame-aligned duration for the current session format."""
        if duration_us <= 0:
            return b""
        bytes_per_sample = max(1, int(self._pcm_format.bit_depth // 8))
        frame_size = bytes_per_sample * int(self._pcm_format.channels)
        samples = max(0, round((duration_us / 1_000_000) * int(self._pcm_format.sample_rate)))
        return b"\x00" * (samples * frame_size)

    def _is_live_source(self, media: PlayerMedia) -> bool:
        """
        Return whether this media is fed to the server at playback pace.

        Radio and live sources always are; a track is when its provider hands
        over the audio just-in-time (a queue flow of such items included, which
        the media type cannot express because it names the first item).

        :param media: The media about to be played.
        """
        if media.media_type in _LIVE_MEDIA_TYPES:
            return True
        if not media.source_id or not media.queue_item_id:
            return False
        queue_item = self.player.mass.player_queues.get_item(media.source_id, media.queue_item_id)
        return bool(
            queue_item and queue_item.streamdetails and queue_item.streamdetails.is_realtime
        )
