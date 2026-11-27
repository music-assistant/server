"""Playback session coordinator for Sendspin players."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from itertools import groupby
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from aiosendspin.models.types import AudioCodec as SendspinAudioCodec
from aiosendspin.server.audio import AudioFormat as SendspinAudioFormat
from aiosendspin.server.push_stream import MAIN_CHANNEL, PushStream, StreamStoppedError
from aiosendspin.server.roles.player.v1 import PlayerV1Role
from music_assistant_models.enums import ContentType, MediaType, SpeakerChannel
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.constants import (
    CONF_FLOW_MODE_SAMPLE_RATE,
    FLOW_MODE_SAMPLE_RATE_BIT_PERFECT,
    FLOW_MODE_SAMPLE_RATE_SMART,
)
from music_assistant.controllers.streams.audio import overlay_mixed_in
from music_assistant.controllers.streams.audio_processing import get_media_session_id
from music_assistant.helpers.audio import arriving_audio_format, iter_pcm_slices, resample_pcm_audio
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.helpers.util import import_module_in_thread
from music_assistant.models.media_capabilities import AudioSourceRefusedError
from music_assistant.models.music_provider import ProviderStreamLimitError
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterable

    from music_assistant_models.config_entries import PlayerConfig
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant.controllers.streams.audio_processing import AudioOutputPlan
    from music_assistant.controllers.streams.controller import AudioSourceStream
    from music_assistant.helpers.dsp import ComplexFilter

    from .player import SendspinPlayer
    from .provider import SendspinProvider


# Default session PCM format (MA-side and wire) used until _run_playback picks the
# session rate. Same sample format expressed in both MA and Sendspin types.
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
# lasting cost. A track from a source that feeds just-in-time (StreamDetails.is_realtime)
# is buffered too. Its longer lead lasts the whole stream but only delays the start,
# while the live send-ahead makes a member that needs a long startup lead drop the
# start of the track.
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
# PCM a buffered source queues before its first commit, which carries all of it. That schedules
# this much audio ahead of real time at once, so neither a producer that lags right after the
# start nor a slow first commit moves the play timeline.
_START_QUEUE_US = 500_000
# Commits from the start of a stream in which a moved play timeline is logged.
_START_LOG_COMMITS = 20
# How long the session waits, after its audio played out, for a next item the queue still names.
_LATE_HANDOVER_TIMEOUT_S = 10
# How long before its audio plays out a session stops waiting for an item added after its last
# one, which leaves the end of that audio the lead time the players need.
_LAST_AUDIO_LEAD_US = 5_000_000
# Start join promotion once catchup processor lag is within this window of the history tail.
_JOIN_PROMOTE_ARM_WINDOW_US = 2_000_000
# Accept catchup output within this margin of the promotion target.
_JOIN_PROMOTE_TOLERANCE_US = 50_000
# Abort join catchup if promotion hasn't completed within this.
_JOIN_PROMOTION_TIMEOUT_S = 15.0
# Anchor movement that publishes elapsed time and refreshes beats right away.
ANCHOR_REBASE_SIGNIFICANT_US = 500_000
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
    timeline_start_us: int
    pcm_format: AudioFormat


@dataclass(slots=True)
class _PendingChunk:
    pcm: bytes
    duration_us: int
    # the later item whose first chunk this is
    starts_item: PlayerMedia | None = None


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
    filter_params: tuple[str | ComplexFilter, ...]

    @property
    def signature(self) -> tuple[bool, tuple[str | ComplexFilter, ...]]:
        return (self.requires_transform, self.filter_params)


@dataclass(slots=True)
class _MemberPipeline:
    player_id: str
    channel_id: UUID
    config: _PipelineConfig
    processor: _BufferedFfmpegProcessor | None = None
    ready: bool = False
    push_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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

    The first commit of a buffered source waits until ``_START_QUEUE_US`` of audio is
    queued and carries all of it, so the start is scheduled ahead of real time.

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
        self._produced_audio_us = 0
        self._history: deque[_HistoryChunk] = deque()
        self._join_catchup: dict[str, _JoinCatchupState] = {}
        self._pipeline_config_cache: dict[str, _PipelineConfig] = {}
        self._preassigned_channels: dict[str, UUID] = {}
        self._mapping_dirty = True
        self._cancel_requested = False
        # PCM formats are refreshed at the start of every _run_playback, and at each
        # later item when items keep their own format: the wire/MA-side rate follows the
        # source within the range of rates the members play (see _select_session_pcm_formats),
        # F32 is always used internally for DSP headroom.
        self._pcm_format: AudioFormat = _DEFAULT_PCM_FORMAT
        self._sendspin_pcm_format: SendspinAudioFormat = _DEFAULT_SENDSPIN_PCM_FORMAT
        self._queue_id: str | None = None
        self._queue_session_id: str | None = None
        # stream details of the item the session sends, which the players' sample rate follows
        self._item_streamdetails: StreamDetails | None = None
        # whether the session blends the audio of its items, and whether it mixes an overlay in
        self._start_mixes: tuple[bool, bool] = (False, False)
        # the session's items in play order, each with where its audio starts in the session audio
        self._item_starts: list[tuple[int, PlayerMedia]] = []
        # the opened item whose source sent no audio yet
        self._opening_item: PlayerMedia | None = None
        # the item the queue handed over to play after the last opened one
        self._next_media: PlayerMedia | None = None
        self._next_media_ready = asyncio.Event()
        # the session sent the end of its audio, so it plays no further item
        self._audio_ended = False

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

    def item_start_us(self, queue_item_id: str, now_us: int) -> int | None:
        """
        Return the server-clock time where the given queue item's audio starts playing.

        :param queue_item_id: The queue item to look up among this session's items.
        :param now_us: The current server-clock time, which picks the pass of a repeated item
            that plays by then.
        """
        if self._timeline_start_us is None:
            return None
        start_us: int | None = None
        for offset_us, media in self._item_starts:
            item_start_us = self._timeline_start_us + offset_us
            if media.queue_item_id == queue_item_id and (
                start_us is None or item_start_us <= now_us
            ):
                start_us = item_start_us
        return start_us

    def upcoming_item(self) -> tuple[PlayerMedia, PlayerMedia, int] | None:
        """Return the item playing now, the item after it, and the server-clock time it starts."""
        push_stream = self._push_stream
        if push_stream is None or (anchor_us := self._timeline_start_us) is None:
            return None
        playing_offset_us, playing = self._playing_item(anchor_us, push_stream.now_us())
        if playing is None:
            return None
        return next(
            (
                (playing, media, anchor_us + offset_us)
                for offset_us, media in self._started_items()
                if offset_us > playing_offset_us
            ),
            None,
        )

    def set_next_media(self, media: PlayerMedia) -> bool:
        """
        Hand over the item to play after the one playing now.

        :param media: The item that follows the playing one.
        :return: False when the session cannot play it next, because another item was already
            sent ahead in its place or the end of its audio was sent. Only a restart from the
            current position plays it then.
        """
        if self._audio_ended:
            return False
        current = self.player.current_media
        opened = [item for _, item in self._item_starts]
        if opened and opened[-1] is not current and any(item is current for item in opened):
            # the last opened item does not play yet, so it was sent ahead
            sent_ahead = opened[-1]
            if media.queue_item_id != sent_ahead.queue_item_id:
                return False
            # the same item is handed over again, unless the queue repeats it after itself
            if self._following_item_id(sent_ahead) != media.queue_item_id:
                return True
        self._next_media = media
        self._next_media_ready.set()
        return True

    # -- Public API ------------------------------------------------------------

    async def transfer_to(self, new_player: SendspinPlayer) -> None:
        """
        Transfer session ownership to a new player.

        Used during dynamic leader switching to keep the push stream alive
        while the old leader is removed from the sendspin group. The PushStream
        and all internal state (pipelines, history, join-catchup) stay intact;
        only the owning player reference is updated and the new leader leaves the member set.

        Cleans up the old leader's pipeline/channel state so its FFmpeg
        processor is released.

        :param new_player: The SendspinPlayer that will take over as session owner.
        """
        old_leader_id = self.player.protocol_id
        # the session follows its items through the player's current media
        new_player._attr_current_media = self.player.current_media
        self.player = new_player
        # Release the old leader's DSP pipeline -- it's no longer in the group
        # and _refresh_member_mappings won't touch it since it only iterates
        # current members + the (new) leader.
        async with self._state_lock:
            pipeline = self._member_pipelines.pop(old_leader_id, None)
            self._pipeline_config_cache.pop(old_leader_id, None)
            self._preassigned_channels.pop(old_leader_id, None)
            # Members exclude the leader, so the new leader's next sync_members won't remove it.
            self._members.discard(new_player.protocol_id)
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
        self._item_starts = []
        self._audio_ended = False
        self._next_media = None
        self._next_media_ready.clear()
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
            self._pipeline_config_cache.clear()
            self._preassigned_channels.clear()

    async def get_session_sample_rate(self, start_streamdetails: StreamDetails) -> int:
        """
        Return the sample rate this group picks for a session starting with an item.

        :param start_streamdetails: Stream details of the first item of the session.
        """
        provider = cast("SendspinProvider", self.player.provider)
        member_rates: list[int] = []
        for client in self.player.api.group.clients:
            if provider.is_silent_client(client.client_id):
                continue
            session_format = await provider.get_client_session_format(
                client.client_id, start_streamdetails
            )
            if session_format is not None:
                member_rates.append(_counted_sample_rate(_to_audio_format(*session_format)))
            elif rate := self._get_member_sample_rate(client.client_id):
                member_rates.append(rate)
        return self._pick_session_sample_rate(
            arriving_audio_format(start_streamdetails).sample_rate, member_rates
        )

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
            await self._follow_session_sample_rate([player_id])
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
                # the streams controller logs a source refusing this speaker itself
                if exc is not None and not isinstance(exc, AudioSourceRefusedError):
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
            leader_id = self.player.protocol_id
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
        if not playback_active or not self._is_member(player_id):
            return

        pipeline = await self._sync_member_pipeline(player_id)
        if not pipeline.config.requires_transform:
            return

        await self._stop_join_catchup(player_id)

        ffmpeg_obj = self._create_member_ffmpeg(pipeline.config.filter_params)
        pcm_format = self._pcm_format
        processor = _BufferedFfmpegProcessor(ffmpeg_obj, pcm_format)
        await processor.start()
        # Bounded queue sized to hold the full buffer duration with some headroom.
        queue_size = (_PRODUCER_BUFFER_LIMIT_US // _PRODUCER_SLICE_US) + _PRODUCER_BACKLOG_SIZE
        input_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_size)

        async with self._state_lock:
            history_snapshot = list(self._history)
        # With no history yet, the commit fanout feeds the processor from the first chunk on.
        history_end_us = (
            history_snapshot[-1].start_time_us + history_snapshot[-1].duration_us
            if history_snapshot
            else None
        )
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

        stale = False
        try:
            async with self._state_lock:
                # a format switch or a leave while the processor started makes it stale
                stale = self._pcm_format != pcm_format or not self._is_member(player_id)
                if not stale:
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
                    # Start eagerly so the feed holds the write lock before any commit fanout.
                    snapshot_task = asyncio.create_task(
                        self._feed_join_history(player_id, state, history_snapshot),
                        eager_start=True,
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
        if stale:
            with suppress(Exception):
                await processor.close()
            if self._pcm_format != pcm_format and self._is_member(player_id):
                await self._start_join_catchup(player_id)

    async def _feed_join_history(
        self,
        player_id: str,
        state: _JoinCatchupState,
        history_snapshot: list[_HistoryChunk],
    ) -> None:
        """Feed historical PCM into a join-catchup processor."""
        processor = state.processor
        async with state.write_lock:
            first_history_start_us: int | None = None
            previous_end_us: int | None = None
            for hist_chunk in await self._history_in_session_format(history_snapshot):
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

    async def _history_in_session_format(self, history: list[_HistoryChunk]) -> list[_HistoryChunk]:
        """
        Return history with the audio sent before a format switch converted to the session format.

        :param history: Committed history chunks, oldest first.
        """
        converted: list[_HistoryChunk] = []
        for pcm_format, group in groupby(history, key=lambda chunk: chunk.pcm_format):
            chunks = list(group)
            if pcm_format == self._pcm_format:
                converted.extend(chunks)
                continue
            start_us = chunks[0].start_time_us
            frame_size = pcm_format.bit_depth // 8 * pcm_format.channels
            pcm = bytearray()
            previous_end_us = start_us
            for chunk in chunks:
                gap_frames = round(
                    (chunk.start_time_us - previous_end_us) * pcm_format.sample_rate / 1_000_000
                )
                pcm += bytes(max(0, gap_frames) * frame_size)
                pcm += chunk.pcm
                previous_end_us = chunk.start_time_us + chunk.duration_us
            resampled = b"".join(
                [
                    resampled_chunk
                    async for resampled_chunk in resample_pcm_audio(
                        bytes(pcm), pcm_format, self._pcm_format
                    )
                ]
            )
            converted.append(
                _HistoryChunk(
                    start_time_us=start_us,
                    duration_us=previous_end_us - start_us,
                    pcm=resampled,
                    timeline_start_us=chunks[0].timeline_start_us,
                    pcm_format=self._pcm_format,
                )
            )
        return converted

    async def _stop_join_catchup(self, player_id: str) -> None:
        """Stop and remove dedicated join catchup processor for one player."""
        async with self._state_lock:
            state = self._join_catchup.pop(player_id, None)
        if state is None:
            return
        tasks = [state.writer_task, state.drainer_task]
        if state.snapshot_task is not None:
            tasks.append(state.snapshot_task)
        for task in tasks:
            task.cancel()
        try:
            # a cancel of the caller still reaches it through gather
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
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
        # Hold live pushes until the writer has flushed the PCM queued ahead of them.
        async with pipeline.push_lock:
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

    async def _run_playback(self, media: PlayerMedia) -> None:  # noqa: C901, PLR0915
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
        source_stream: AudioSourceStream | None = None
        setup_done = False
        try:
            # A line-in has no queue item, so it is opened first to learn its format.
            if media.media_type == MediaType.AUDIO_SOURCE:
                source_stream = await self.player.mass.streams.open_audio_source_stream(
                    media, self.player.protocol_id
                )
            # let automatic Sendspin formats follow the first item's sample rate, then
            # refresh the session PCM format from the members' preferred outputs before
            # building any pipelines; member ffmpeg pipelines and pre-computed filter
            # params depend on this rate so the cache must also be cleared
            self._item_streamdetails = (
                source_stream.streamdetails
                if source_stream is not None
                else self._get_item_streamdetails(media)
            )
            self._start_mixes = self._get_start_mixes(media)
            await self._follow_session_sample_rate(
                [client.client_id for client in self.player.api.group.clients]
            )
            self._pcm_format, self._sendspin_pcm_format = self._select_session_pcm_formats()
            flow_restart = self._flow_restart_of_this_playback()
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
                self._mapping_dirty = True
            setup_done = True
        except Exception:
            # A track change stops the previous stream without stream/end, so a failed
            # setup has to end this one. Cancellation propagates untouched, since there
            # the successor keeps the stream.
            if push_stream is not None:
                with suppress(Exception):
                    push_stream.stop()
            await self._reset_session_state()
            raise
        finally:
            if not setup_done and source_stream is not None:
                await source_stream.aclose()
        # Bounded queue between producer (stream reader) and consumer (committer).
        pending_chunks: asyncio.Queue[_PendingChunk | None] = asyncio.Queue(
            maxsize=_PRODUCER_BACKLOG_SIZE
        )
        # Shadow deque mirroring pending_chunks for join-catchup backlog peeking.
        pending_backlog: deque[_PendingChunk] = deque()
        pending_duration_us = 0
        # Set once the first commit may go out. Live audio has nothing ahead to wait for.
        start_queued = asyncio.Event()
        if self._plays_live_audio(media):
            start_queued.set()
        # where the next produced chunk starts in the session audio
        produced_us = 0
        last_elapsed_update_s: float | None = None
        reported_anchor_us: int | None = None
        reported_upcoming: tuple[PlayerMedia | None, float | None] = (None, None)

        async def _produce_pending_chunks() -> None:
            item_media: PlayerMedia | None = media
            while item_media is not None:
                item_start_us = produced_us
                self._opening_item = item_media
                try:
                    await _produce_item_chunks(item_media)
                    # A source shorter than the start queue plays what it has.
                    start_queued.set()
                finally:
                    # an item that ends without audio still takes its turn, so the queue moves on
                    self._opening_item = None
                if (
                    produced_us == item_start_us
                    and len(self._item_starts) > 1
                    and item_media.source_id
                    and item_media.queue_item_id
                ):
                    # The queue hands over the item after this one once this one loads, which
                    # its stream only reports with its first audio.
                    self.player.mass.player_queues.track_loaded_in_buffer(
                        item_media.source_id, item_media.queue_item_id
                    )
                item_media = await _next_item_media(item_media)
            self._audio_ended = True

        async def _next_item_media(item_media: PlayerMedia) -> PlayerMedia | None:
            while True:
                if not self._next_media_ready.is_set():
                    if not self._takes_handovers(item_media):
                        return None
                    if self._following_item_id(item_media) is None:
                        # nothing follows, so the end of the audio goes out with its full lead
                        await _flush_member_pipelines()
                    # the queue hands over the next item once the last one plays, or once one is
                    # added after it, so wait for it until the audio sent so far is about to end
                    await _wait_for_handover(_play_out(_LAST_AUDIO_LEAD_US))
                if not self._next_media_ready.is_set() and self._following_item_id(item_media):
                    # the item after a short one is handed over once that one plays, which can be
                    # after its audio played out, so it follows after a gap
                    await _flush_member_pipelines()
                    await _wait_for_handover(_late_handover())
                self._next_media_ready.clear()
                next_media, self._next_media = self._next_media, None
                # drop an item handed over before the queue changed, and wait for the one that
                # follows now, which the queue hands over only when there is one
                if next_media is None or next_media.queue_item_id == self._following_item_id(
                    item_media
                ):
                    return next_media

        async def _wait_for_handover(deadline: Coroutine[Any, Any, None]) -> None:
            waiters = {
                asyncio.create_task(self._next_media_ready.wait()),
                asyncio.create_task(deadline),
            }
            try:
                await asyncio.wait({*waiters, commit_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

        async def _late_handover() -> None:
            await _play_out(0)
            await asyncio.sleep(_LATE_HANDOVER_TIMEOUT_S)

        async def _play_out(lead_us: int) -> None:
            # Timed from the clock: a member's DSP holds back the last chunk until more
            # audio follows, so the commits never catch up while this waits.
            while True:
                anchor_us = self._timeline_start_us
                # before the first commit, nothing played yet
                remaining_us = (
                    produced_us
                    if anchor_us is None
                    else anchor_us + produced_us - push_stream.now_us()
                )
                if remaining_us <= lead_us:
                    return
                await asyncio.sleep((remaining_us - lead_us) / 1_000_000)

        async def _produce_item_chunks(item_media: PlayerMedia) -> None:
            nonlocal pending_duration_us, produced_us
            starts_item = item_media if self._item_starts else None
            self._item_starts.append((produced_us, item_media))
            audio_source: AsyncGenerator[bytes] | None = None
            completed = False
            try:
                await self._load_item_streamdetails(item_media)
                if starts_item is not None:
                    await _switch_to_item_format(item_media)
                audio_source = (
                    source_stream.stream(self._pcm_format)
                    if source_stream is not None
                    else self.player.mass.streams.get_stream(
                        item_media,
                        self._pcm_format,
                        self.player.protocol_id,
                        flow_restart=flow_restart,
                    )
                )
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
                        pending = _PendingChunk(
                            pcm=slice_chunk, duration_us=duration_us, starts_item=starts_item
                        )
                        starts_item = self._opening_item = None
                        await pending_chunks.put(pending)
                        pending_backlog.append(pending)
                        pending_duration_us += duration_us
                        # A full queue blocks the producer, so it cannot grow any further.
                        if pending_duration_us >= _START_QUEUE_US or pending_chunks.full():
                            start_queued.set()
                        produced_us += duration_us
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
            except MusicAssistantError as err:
                # an item that fails after another one keeps that one's audio playing
                if len(self._item_starts) == 1:
                    raise
                self.player.logger.warning("Skipping %s: %s", item_media.uri, err)
            finally:
                if not completed and audio_source is not None:
                    close_task = asyncio.create_task(audio_source.aclose())
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError:
                        await close_task
                        raise

        async def _switch_to_item_format(item_media: PlayerMedia) -> None:
            if (streamdetails := self._get_item_streamdetails(item_media)) is None:
                return
            sample_rate = await self._later_item_sample_rate(streamdetails)
            if sample_rate == self._pcm_format.sample_rate:
                return
            pcm_format, sendspin_pcm_format = _session_pcm_formats(sample_rate)
            # the member DSP processes run at one rate, so they play out and restart at the new one
            # and a join catch-up promoted while they drain gets its EOF too
            self._producer_eof_sent = True
            await self._write_eof_to_member_pipelines()
            await _commit_queued_chunks()
            async with self._state_lock:
                self._pcm_format = pcm_format
                self._sendspin_pcm_format = sendspin_pcm_format
                self._producer_eof_sent = False
            await self._restart_member_pipelines()
            self.player.logger.debug(
                "Sendspin session PCM format: %d Hz / F32", pcm_format.sample_rate
            )

        async def _flush_member_pipelines() -> None:
            # the member DSP processes hold back their last chunk until their input ends
            self._producer_eof_sent = True
            await self._write_eof_to_member_pipelines()
            await _commit_queued_chunks()
            async with self._state_lock:
                self._producer_eof_sent = False
            await self._restart_member_pipelines()

        async def _commit_queued_chunks() -> None:
            drained = asyncio.create_task(pending_chunks.join())
            try:
                await asyncio.wait({drained, commit_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                drained.cancel()
                await asyncio.gather(drained, return_exceptions=True)

        anchor_move_task_id = f"sendspin_anchor_move_{uuid4().hex}"
        first_heard_task_id = f"sendspin_first_heard_{uuid4().hex}"
        first_heard_scheduled = False
        first_heard_reported = False
        report_when_heard = False
        item_start_task_id = f"sendspin_item_start_{uuid4().hex}"

        async def _report_timeline(anchor_us: int, now_us: int) -> None:
            nonlocal last_elapsed_update_s, reported_anchor_us, reported_upcoming
            nonlocal first_heard_scheduled, first_heard_reported, report_when_heard
            # The first commit reports too, since beats published before it were timed
            # from an estimated start. Later moves are compared against the last reported
            # anchor so small ones add up.
            if (
                reported_anchor_us is None
                or abs(anchor_us - reported_anchor_us) >= ANCHOR_REBASE_SIGNIFICANT_US
            ):
                timeline_changed = True
                reported_anchor_us = anchor_us
            else:
                timeline_changed = False
            stream_position_s = (now_us - anchor_us) / 1_000_000
            heard = False
            if not first_heard_reported and stream_position_s < 0:
                # The first commits happen the players' lead time before position 0 plays,
                # so the position published now runs ahead by that much. Publish it again
                # once the audio is heard: the later updates are too small a correction
                # to re-anchor a published position by themselves. Follow the anchor until
                # then: a new stream can start out on the old one's schedule.
                if not first_heard_scheduled or timeline_changed or report_when_heard:
                    first_heard_scheduled = True
                    self.player.mass.call_later(
                        -stream_position_s, _report_when_heard, task_id=first_heard_task_id
                    )
            elif not first_heard_reported and first_heard_scheduled:
                heard = True
            item_offset_us, item_media = self._playing_item(anchor_us, now_us)
            shown = next(
                (
                    (index, offset_us)
                    for index, (offset_us, media) in enumerate(self._item_starts)
                    if media is self.player.current_media
                ),
                None,
            )
            # An item only takes over from an earlier item of this session the player shows.
            item_changed = shown is not None and any(
                media is item_media for _, media in self._item_starts[shown[0] + 1 :]
            )
            if item_changed:
                self.player._attr_current_media = item_media
            elif shown is not None:
                # A schedule moved on after the audio ran out can put an earlier item at now.
                item_offset_us = shown[1]
            elapsed_real_s = max(0.0, (now_us - anchor_us - item_offset_us) / 1_000_000)
            # A rebase or a new item can step the position back, which would keep the gate shut.
            if (
                timeline_changed
                or heard
                or item_changed
                or last_elapsed_update_s is None
                or elapsed_real_s - last_elapsed_update_s >= 1.0
            ):
                last_elapsed_update_s = elapsed_real_s
                self.player._attr_elapsed_time = elapsed_real_s
                self.player._attr_elapsed_time_last_updated = time.time()
                # The correction when the first audio is heard is smaller than what
                # update_state() takes for a moved position, so it has to insist.
                self.player.update_state(force_update=heard)
            if heard:
                first_heard_reported = True
                report_when_heard = False
                self.player.mass.cancel_timer(first_heard_task_id)
                self.player.on_first_audio_heard()
            if timeline_changed:
                self.player.on_flow_timeline_changed()
            upcoming = self._upcoming_item_start()
            if (
                timeline_changed
                or upcoming[0] is not reported_upcoming[0]
                or upcoming[1] != reported_upcoming[1]
            ):
                reported_upcoming = upcoming
                self.player.on_upcoming_item_changed()
            # A move queued behind the playing audio must apply even if no commit follows.
            async with self._state_lock:
                move_start_us = self._queued_move_start_us_locked(now_us, reported_anchor_us)
            if move_start_us is None:
                self.player.mass.cancel_timer(anchor_move_task_id)
            else:
                self.player.mass.call_later(
                    (move_start_us - now_us) / 1_000_000,
                    _apply_queued_move,
                    task_id=anchor_move_task_id,
                )
            # The next item can start while nothing is committed, like after the last item.
            if (next_item_start_us := self._next_item_start_us(anchor_us, now_us)) is None:
                self.player.mass.cancel_timer(item_start_task_id)
            else:
                self.player.mass.call_later(
                    (next_item_start_us - now_us) / 1_000_000,
                    _apply_item_start,
                    task_id=item_start_task_id,
                )

        async def _report_when_heard() -> None:
            nonlocal report_when_heard
            now_us = push_stream.now_us()
            async with self._state_lock:
                if not self._history:
                    return
                anchor_us = self._playing_timeline_start_us_locked(now_us)
            report_when_heard = True
            await _report_timeline(anchor_us, now_us)

        async def _apply_queued_move() -> None:
            now_us = push_stream.now_us()
            async with self._state_lock:
                if not self._history:
                    return
                anchor_us = self._playing_timeline_start_us_locked(now_us)
                self._timeline_start_us = anchor_us
            await _report_timeline(anchor_us, now_us)

        async def _apply_item_start() -> None:
            if (anchor_us := self._timeline_start_us) is not None:
                await _report_timeline(anchor_us, push_stream.now_us())

        def _take_start_queue(first: _PendingChunk) -> tuple[_PendingChunk, bool]:
            """
            Join the audio queued for the start of the stream to its first chunk.

            :param first: The first chunk of the stream, already taken from the queue.
            :return: The joined chunk, and whether the source ended with it.
            """
            nonlocal pending_duration_us
            chunks = [first]
            queued_us = first.duration_us
            source_ended = False
            while queued_us < _START_QUEUE_US and not pending_chunks.empty():
                pending = pending_chunks.get_nowait()
                if pending is None:
                    source_ended = True
                    break
                pending_chunks.task_done()
                pending_backlog.popleft()
                pending_duration_us = max(0, pending_duration_us - pending.duration_us)
                chunks.append(pending)
                queued_us += pending.duration_us
            if len(chunks) == 1:
                return first, source_ended
            pcm = b"".join(chunk.pcm for chunk in chunks)
            return _PendingChunk(pcm, self._duration_us(pcm, self._pcm_format)), source_ended

        async def _commit_pending_chunks() -> None:  # noqa: PLR0915
            nonlocal pending_duration_us
            commit_count = 0
            previous_timeline_start_us: int | None = None
            # Clock times of the commit loop, for the debug line about a moved timeline.
            first_lead_us = 0
            previous_commit_called_us = 0
            await start_queued.wait()
            source_ended = False
            while not source_ended:
                waiting_since_us = push_stream.now_us()
                pending = await pending_chunks.get()
                if pending is None:
                    break
                pending_backlog.popleft()
                pending_duration_us = max(0, pending_duration_us - pending.duration_us)
                # a member that joined before the start gets its audio in the commits after
                # the first, so those are left to come
                if commit_count == 0 and not self._join_catchup:
                    pending, source_ended = _take_start_queue(pending)
                await self._inject_ready_join_historical(push_stream, pending_backlog, pending.pcm)
                if pending.starts_item is not None:
                    client_ids = [client.client_id for client in self.player.api.group.clients]
                    if streamdetails := self._get_item_streamdetails(pending.starts_item):
                        # each player's new format starts where the audio it was sent ends
                        self._item_streamdetails = streamdetails
                        await self._follow_session_sample_rate(client_ids, later_item=True)
                    for client_id in client_ids:
                        self._get_output_plan(client_id, pending.starts_item.queue_item_id)
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
                commit_called_us = push_stream.now_us()
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
                    # aiosendspin moves the schedule forward when the producer falls behind.
                    timeline_start_us=int(commit_start_us) - self._produced_audio_us,
                    pcm_format=self._pcm_format,
                )
                async with self._state_lock:
                    # A commit in flight when the next playback replaced this stream still
                    # returns, stamped from the old stream's buffer (up to 30 s ahead). The
                    # session state belongs to the new stream by then: leave it alone.
                    if push_stream.is_stopped or self._push_stream is not push_stream:
                        self.player.logger.debug(
                            "Stopping commit loop: the push stream was replaced"
                        )
                        break
                    self._history.append(committed_history_chunk)
                    anchor_us = self._playing_timeline_start_us_locked(commit_now_us)
                    self._timeline_start_us = anchor_us
                    self._produced_audio_us += pending.duration_us
                    self._prune_history_locked(commit_now_us)
                commit_count += 1
                timeline_start_us = committed_history_chunk.timeline_start_us
                lead_us = int(commit_start_us) - commit_called_us
                if commit_count == 1:
                    first_lead_us = lead_us
                elif (
                    commit_count <= _START_LOG_COMMITS
                    and previous_timeline_start_us is not None
                    # anything smaller is rounding
                    and abs(timeline_start_us - previous_timeline_start_us) >= 1_000
                ):
                    self.player.logger.debug(
                        "Play timeline moved %+.1f ms at commit %d of the stream: the commit "
                        "before it and what follows it took %.1f ms, then getting the audio "
                        "of this one ready %.1f ms; scheduled %.1f ms ahead, the first commit "
                        "%.1f ms",
                        (timeline_start_us - previous_timeline_start_us) / 1000,
                        commit_count,
                        (waiting_since_us - previous_commit_called_us) / 1000,
                        (commit_called_us - waiting_since_us) / 1000,
                        lead_us / 1000,
                        first_lead_us / 1000,
                    )
                previous_timeline_start_us = timeline_start_us
                previous_commit_called_us = commit_called_us
                await _report_timeline(anchor_us, commit_now_us)
                await self._fanout_history_chunk_to_join_processors(committed_history_chunk)
                pending_chunks.task_done()

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
                await self._write_eof_to_member_pipelines()
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
                    # resamplers and encoders hold the end of the audio back until flushed
                    try:
                        await push_stream.flush_audio()
                    except Exception:
                        self.player.logger.exception("Failed to flush the end of the audio")
                    await self._wait_for_buffer_drain()
                except asyncio.CancelledError:
                    # New playback interrupted the drain — treat as
                    # non-clean stop so we skip group.stop() below
                    # and let the new playback handle the transition.
                    producer_stopped_cleanly = False
            for task_id in (anchor_move_task_id, first_heard_task_id, item_start_task_id):
                self.player.mass.cancel_timer(task_id)
                self.player.mass.cancel_task(task_id)
            with suppress(Exception):
                # Same condition as the group.stop() below, so we snapshot on exactly the
                # paths where a group STOP is already emitted. That stop resets the reported
                # position to 0 (spec: stop rewinds), so the snapshot only carries the
                # position if the stop below raises.
                self._stop_push_stream(
                    snapshot_progress=producer_stopped_cleanly and not self._cancel_requested,
                )
            await self._clear_join_catchup()
            await self._clear_member_pipelines()
            await self._reset_session_state()
            self.player.on_upcoming_item_changed()
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
           catchup processor is promoted to the member's live DSP pipeline and its
           output up to the live tail is injected as the member's history.
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
            pipeline = await self._sync_member_pipeline(player_id)
            async with self._state_lock:
                live_tail_us = (
                    self._history[-1].start_time_us + self._history[-1].duration_us
                    if self._history
                    else target_end_us
                )
            await self._prefeed_pending_backlog_for_join(state, current_pcm, pending_backlog)
            await self._promote_join_catchup_processor(player_id, pipeline, target_end_us)
            if pipeline.processor is not state.processor:
                continue
            # Read after promotion so its flush has written the newest committed PCM to ffmpeg.
            transformed_history = await state.processor.read_duration_us(
                live_tail_us - first_history_start_us
            )
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
            injected_any = True
        return injected_any

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
        await self._sync_member_pipeline(self.player.protocol_id)

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
                if self._push_stream is not None and not self.player.requires_flow_mode:
                    # a player switching format resumes from this cache where its sent audio ends
                    self._push_stream.enable_pcm_cache_for_channel(channel_id)
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
        provider = cast("SendspinProvider", self.player.provider)
        if provider.is_silent_client(player_id):
            # a client that renders nothing takes the audio as it is
            return _PipelineConfig(requires_transform=False, filter_params=())
        # a speaker of a multi-channel player plays with the settings of that player
        protocol_id = provider.speaker_output(player_id)[0]
        # the DSP settings are those of the player the member's protocol belongs to
        member = self.player.mass.players.get_player_by_protocol(protocol_id)
        dsp_config = self.player.mass.config.get_player_dsp_config(
            member.player_id if member else protocol_id
        )
        dsp_enabled = bool(dsp_config.enabled)
        # the session format is that of the newest item opened, and a flow stream has one
        # output for all the items it carries
        newest = self._item_starts[-1][1] if self._item_starts else None
        queue_item_id = (
            newest.queue_item_id if newest is not None and self._takes_handovers(newest) else None
        )
        output_plan = self._get_output_plan(player_id, queue_item_id)
        filter_params = tuple(output_plan.filter_params) if output_plan is not None else ()
        # a ComplexFilter (e.g. convolution) is never a plain string, so it always counts
        custom_filter_graph = any(
            not isinstance(param, str) or param.strip() for param in filter_params
        )
        requires_transform = dsp_enabled or custom_filter_graph
        return _PipelineConfig(
            requires_transform=requires_transform,
            filter_params=filter_params,
        )

    def _get_output_plan(self, player_id: str, queue_item_id: str | None) -> AudioOutputPlan | None:
        """
        Return a player's output plan at the session format, and report it for a queue item.

        :param player_id: The player the plan is for.
        :param queue_item_id: The queue item the plan is reported for, or None for the session.
        """
        provider = cast("SendspinProvider", self.player.provider)
        if provider.is_silent_client(player_id):
            return None
        # a speaker of a multi-channel player plays with the settings of that player
        protocol_id, channel, with_subwoofer = provider.speaker_output(player_id)
        try:
            return self.player.mass.streams.audio.get_player_output_plan(
                protocol_id,
                self._pcm_format,
                self._get_member_output_format(player_id),
                handoff_format=self._pcm_format,
                # a subwoofer plays along with the main speakers, which report for their player
                queue_id=self._queue_id if channel != SpeakerChannel.LFE else None,
                session_id=self._queue_session_id,
                queue_item_id=queue_item_id,
                channel=channel,
                with_subwoofer=with_subwoofer,
            )
        except Exception as err:  # noqa: BLE001 - any failure means no dedicated dsp channel
            self.player.logger.debug("Could not build the output plan for %s: %s", player_id, err)
            return None

    def _get_item_streamdetails(self, media: PlayerMedia) -> StreamDetails | None:
        """Return the resolved stream details of a queue item of this session."""
        if not media.source_id or not media.queue_item_id:
            return None
        queue_item = self.player.mass.player_queues.get_item(media.source_id, media.queue_item_id)
        return queue_item.streamdetails if queue_item else None

    def _get_start_mixes(self, media: PlayerMedia) -> tuple[bool, bool]:
        """
        Return whether a session blends the audio of its items, and whether it mixes an overlay in.

        :param media: The media the session starts with.
        """
        if not media.source_id or not media.queue_item_id:
            return False, False
        mass = self.player.mass
        queue = mass.player_queues.get(media.source_id)
        queue_item = mass.player_queues.get_item(media.source_id, media.queue_item_id)
        if queue is None or queue_item is None:
            return False, False
        # a station or a line-in is a stream of its own, not the flow stream of the queue
        flow_mode = queue_item.media_type not in (MediaType.RADIO, MediaType.AUDIO_SOURCE)
        return (
            flow_mode and mass.streams.flow_stream_blends_items(queue, queue_item),
            overlay_mixed_in(queue, queue_item, flow_mode=flow_mode),
        )

    async def _load_item_streamdetails(self, media: PlayerMedia) -> None:
        """Resolve the stream details of a queue item that has none yet."""
        if not media.source_id or not media.queue_item_id:
            return
        queue_item = self.player.mass.player_queues.get_item(media.source_id, media.queue_item_id)
        if queue_item is not None and queue_item.streamdetails is None:
            try:
                await self.player.mass.streams.audio.get_stream_details(queue_item=queue_item)
            except MusicAssistantError as err:
                # a stream slot that is taken is no failure of the source
                if not isinstance(err, ProviderStreamLimitError):
                    self.player.mass.player_queues.report_source_failure(
                        queue_item, get_media_session_id(media)
                    )
                raise

    async def _follow_session_sample_rate(
        self, player_ids: Iterable[str], *, later_item: bool = False
    ) -> None:
        """
        Let the given players' Sendspin formats follow the sample rate of this session.

        :param player_ids: The players to update.
        :param later_item: The session's item follows another one the players played.
        """
        from .player import SendspinPlayer  # noqa: PLC0415 - player.py imports this module

        crossfade_enabled, overlay_active = self._start_mixes
        for player_id in player_ids:
            player = self.player.provider.get_protocol(player_id)
            if isinstance(player, SendspinPlayer):
                await player.follow_session_sample_rate(
                    self._item_streamdetails,
                    crossfade_enabled=crossfade_enabled,
                    overlay_active=overlay_active,
                    later_item=later_item,
                )

    def _select_session_pcm_formats(self) -> tuple[AudioFormat, SendspinAudioFormat]:
        """
        Pick the session PCM format (MA-side + wire) from the members' preferred formats.

        F32 is always used for DSP headroom. The sample rate is the rate of the session's
        first item, kept within the lowest and the highest rate the members play; without
        a known first item rate, the leader's rate takes its place. A member with a lossy
        output codec counts with at most 48 kHz, as higher rates yield no perceivable
        quality gain there. Members playing another rate than the session are resampled
        on the receiving side.
        """
        provider = cast("SendspinProvider", self.player.provider)
        member_rates = [
            rate
            for client in self.player.api.group.clients
            if not provider.is_silent_client(client.client_id)
            and (rate := self._get_member_sample_rate(client.client_id))
        ]
        streamdetails = self._item_streamdetails
        sample_rate = self._pick_session_sample_rate(
            arriving_audio_format(streamdetails).sample_rate if streamdetails else 0,
            member_rates,
        )
        return _session_pcm_formats(sample_rate)

    async def _later_item_sample_rate(self, streamdetails: StreamDetails) -> int:
        """
        Return the session sample rate for an item that follows another one in the session.

        :param streamdetails: Stream details of the item.
        """
        return later_item_sample_rate(
            self.player.config,
            await self.get_session_sample_rate(streamdetails),
            self._pcm_format.sample_rate,
        )

    def _flow_restart_of_this_playback(self) -> Callable[[StreamDetails], Awaitable[bool]]:
        """
        Return what tells the flow stream of the running playback whether it restarts for an item.

        It restarts when a session starting with the item would run at a higher rate, or at
        another rate while the leader of the group as it is then plays bit-perfect. Call from
        the playback task. Once that playback has ended the answer is no, so a flow that is
        being torn down plays out.
        """
        playback_task = asyncio.current_task()
        stream_rate = self._pcm_format.sample_rate

        async def _flow_restart(streamdetails: StreamDetails) -> bool:
            if (
                playback_task is None
                or playback_task.done()
                or self.playback_task is not playback_task
            ):
                return False
            sample_rate = await self.get_session_sample_rate(streamdetails)
            # a fixed sample rate setting fixes only the leader's own output
            leader_mode = self.player.config.get_value(
                CONF_FLOW_MODE_SAMPLE_RATE, FLOW_MODE_SAMPLE_RATE_SMART
            )
            if leader_mode == FLOW_MODE_SAMPLE_RATE_BIT_PERFECT:
                return sample_rate != stream_rate
            return sample_rate > stream_rate

        return _flow_restart

    def _pick_session_sample_rate(self, source_rate: int, member_rates: list[int]) -> int:
        """
        Return the session sample rate for a source rate and the rates the members play.

        :param source_rate: The sample rate the audio of the first item arrives in, 0 when
            unknown.
        :param member_rates: The rates the members count with, see _get_member_sample_rate.
        """
        if not member_rates:
            return _DEFAULT_PCM_FORMAT.sample_rate
        source_rate = source_rate or self._get_member_sample_rate(self.player.protocol_id) or 0
        return min(max(source_rate, min(member_rates)), max(member_rates))

    def _get_member_output_format(self, player_id: str) -> AudioFormat:
        """
        Return the actual output AudioFormat for a group member.

        Derives the format from the member's sendspin player role (preferred codec
        and format), falling back to the internal PCM format if unavailable.
        """
        return self._get_member_preferred_format(player_id) or self._pcm_format

    def _get_member_sample_rate(self, player_id: str) -> int | None:
        """
        Return the sample rate a group member counts with for the session rate.

        :param player_id: The Sendspin client id of the member.
        :return: The member's preferred rate, at most 48 kHz for a lossy output codec,
            or None for a member without a known preferred format.
        """
        if (output := self._get_member_preferred_format(player_id)) is None:
            return None
        return _counted_sample_rate(output)

    def _get_member_preferred_format(self, player_id: str) -> AudioFormat | None:
        """
        Return the preferred output AudioFormat of a group member, if it has one.

        :param player_id: The Sendspin client id of the member.
        """
        provider = cast("SendspinProvider", self.player.provider)
        client = provider.server_api.get_client(player_id)
        if client is not None:
            for role in client.roles_by_family("player"):
                if isinstance(role, PlayerV1Role):
                    preferred_fmt = role.preferred_format
                    preferred_codec = role.preferred_codec
                    if preferred_fmt is not None and preferred_codec is not None:
                        return _to_audio_format(preferred_fmt, preferred_codec)
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
        return None

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
        async with pipeline.push_lock:
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

    async def _write_eof_to_member_pipelines(self) -> None:
        """Flush the member DSP processes, so their last reads return instead of blocking."""
        _, pipelines = await self._snapshot_active_pipelines()
        for _, pipeline in pipelines:
            async with pipeline.push_lock:
                if pipeline.processor is not None and pipeline.config.requires_transform:
                    with suppress(Exception):
                        await pipeline.processor.write_eof()

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

    async def _restart_member_pipelines(self) -> None:
        """Start the member DSP processes and DSP catch-ups over from the next produced audio."""
        async with self._state_lock:
            self._pipeline_config_cache.clear()
            self._mapping_dirty = True
            joining = list(self._join_catchup)
        await self._clear_join_catchup()
        await self._clear_member_pipelines()
        # a member still catching up starts over in the session's current format
        for player_id in joining:
            try:
                await self._start_join_catchup(player_id)
            except (MusicAssistantError, OSError) as err:
                self.player.logger.warning(
                    "Restarting the DSP catch-up of %s failed: %s", player_id, err
                )

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
            self._produced_audio_us = 0
            self._history.clear()
            self._item_streamdetails = None
            self._start_mixes = (False, False)
            self._item_starts = []
            # Drop cached DSP decisions so next playback reflects latest config.
            self._pipeline_config_cache.clear()

    def _resolve_channel_for_player(self, player_id: str) -> UUID:
        """Channel resolver callback for per-player routing."""
        pipeline = self._member_pipelines.get(player_id)
        if pipeline is not None:
            return pipeline.channel_id
        # Force a fresh config read for pending/unknown joiners so the very
        # first resolution (triggered by add_client) uses up-to-date DSP settings.
        force = player_id not in self._members and player_id != self.player.protocol_id
        config = self._get_pipeline_config_cached(player_id, force_refresh=force)
        if not config.requires_transform:
            return MAIN_CHANNEL
        return self._get_or_create_preassigned_channel(player_id)

    # -- History ---------------------------------------------------------------

    def _playing_timeline_start_us_locked(self, now_us: int) -> int:
        """Return the anchor of the chunk playing at ``now_us``, or of the next one in a gap."""
        # Speakers play audio sent before a schedule move at its old timestamps.
        for chunk in self._history:
            if chunk.start_time_us + chunk.duration_us > now_us:
                return chunk.timeline_start_us
        return self._history[-1].timeline_start_us

    def _playing_item(self, anchor_us: int, now_us: int) -> tuple[int, PlayerMedia | None]:
        """
        Return where the item playing at ``now_us`` starts in the session audio, and its media.

        Before the first item plays, that is the first item.

        :param anchor_us: Server-clock time where the session audio starts.
        :param now_us: The server-clock time to look up.
        """
        playing: tuple[int, PlayerMedia | None] = (0, None)
        for offset_us, media in self._started_items() or self._item_starts[:1]:
            if playing[1] is not None and anchor_us + offset_us > now_us:
                break
            playing = (offset_us, media)
        return playing

    def _started_items(self) -> list[tuple[int, PlayerMedia]]:
        """Return the session's items with their starts, without one that sent no audio yet."""
        return [item for item in self._item_starts if item[1] is not self._opening_item]

    def _upcoming_item_start(self) -> tuple[PlayerMedia | None, float | None]:
        """Return the item after the one playing now, if any, and the file position it starts at."""
        if (upcoming := self.upcoming_item()) is None:
            return None, None
        # A crossfade into the item moves its start once its stream starts.
        streamdetails = self._get_item_streamdetails(upcoming[1])
        return upcoming[1], None if streamdetails is None else streamdetails.seek_position

    def _takes_handovers(self, media: PlayerMedia) -> bool:
        """
        Return whether the queue hands over items to play after the given one.

        :param media: An item of this session.
        """
        if not media.source_id or not media.queue_item_id or self.player.requires_flow_mode:
            return False
        queue_data = self.player.mass.player_queues.queue_data_or_none(media.source_id)
        if queue_data is None or queue_data.session_id != self._queue_session_id:
            # the queue stopped or started something else, which ended this session's stream
            return False
        # a flow stream carries the rest of the queue itself
        return not queue_data.queue.flow_mode

    def _following_item_id(self, media: PlayerMedia) -> str | None:
        """
        Return the queue item the queue hands over to play after the given one, if any.

        :param media: An item of this session.
        """
        if not media.source_id or not media.queue_item_id or not self._takes_handovers(media):
            return None
        following = self.player.mass.player_queues.get_next_item(
            media.source_id, media.queue_item_id
        )
        return None if following is None else following.queue_item_id

    def _next_item_start_us(self, anchor_us: int, now_us: int) -> int | None:
        """
        Return the server-clock time where the first item after ``now_us`` starts, if any.

        :param anchor_us: Server-clock time where the session audio starts.
        :param now_us: The current server-clock time.
        """
        return next(
            (
                anchor_us + offset_us
                for offset_us, _ in self._started_items()
                if anchor_us + offset_us > now_us
            ),
            None,
        )

    def _queued_move_start_us_locked(self, now_us: int, reported_anchor_us: int) -> int | None:
        """Return when the first unplayed chunk that moves the reported anchor starts playing."""
        for chunk in self._history:
            if (
                chunk.start_time_us > now_us
                and abs(chunk.timeline_start_us - reported_anchor_us)
                >= ANCHOR_REBASE_SIGNIFICANT_US
            ):
                return chunk.start_time_us
        return None

    def _prune_history_locked(self, now_monotonic_us: int) -> None:
        """Drop old history chunks that are fully in the past."""
        # History start times are on the push stream clock, so this is what plays now.
        cutoff_us = now_monotonic_us - _HISTORY_KEEP_PAST_US
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

    def _is_member(self, player_id: str) -> bool:
        """Return whether a player is a member of this session or joining it."""
        return player_id in self._members or player_id in self.pending_join_members

    def _is_live_source(self, media: PlayerMedia) -> bool:
        """
        Return whether this media plays as a live source.

        :param media: The media about to be played.
        """
        return media.media_type in _LIVE_MEDIA_TYPES

    def _plays_live_audio(self, media: PlayerMedia) -> bool:
        """
        Return whether the audio of this media arrives live, also when a group streams it.

        :param media: The media about to be played.
        """
        if self._is_live_source(media):
            return True
        if media.media_type != MediaType.FLOW_STREAM or not media.source_id:
            return False
        # A group that streams one source to all its members (a Universal Group) hands each
        # member a flow stream of it, under the id of the group player as source_id.
        group_player = self.player.mass.players.get_player(media.source_id)
        group_protocol = group_player.own_protocol if group_player else None
        group_media = group_protocol.current_media if group_protocol else None
        return group_media is not None and self._is_live_source(group_media)


def later_item_sample_rate(config: PlayerConfig, sample_rate: int, session_sample_rate: int) -> int:
    """
    Return the sample rate a session plays a later item at, per a player's sample rate setting.

    :param config: The config of the player whose setting applies.
    :param sample_rate: The rate a session starting with the item would play at.
    :param session_sample_rate: The rate the session plays at now.
    """
    if (
        config.get_value(CONF_FLOW_MODE_SAMPLE_RATE, FLOW_MODE_SAMPLE_RATE_SMART)
        != FLOW_MODE_SAMPLE_RATE_SMART
    ):
        return sample_rate
    # smart switches a session only up, to a higher-rate item
    return max(sample_rate, session_sample_rate)


def _session_pcm_formats(sample_rate: int) -> tuple[AudioFormat, SendspinAudioFormat]:
    """
    Return the session PCM format (MA-side + wire) at a sample rate.

    :param sample_rate: The sample rate of the session.
    """
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


def _to_audio_format(audio_format: SendspinAudioFormat, codec: SendspinAudioCodec) -> AudioFormat:
    """
    Return the MA AudioFormat of a Sendspin format in a codec.

    :param audio_format: The Sendspin format.
    :param codec: The codec of the format.
    """
    if codec == SendspinAudioCodec.FLAC:
        content_type = ContentType.FLAC
    elif codec == SendspinAudioCodec.OPUS:
        content_type = ContentType.OPUS
    else:
        content_type = ContentType.from_bit_depth(audio_format.bit_depth)
    return AudioFormat(
        content_type=content_type,
        sample_rate=audio_format.sample_rate,
        bit_depth=audio_format.bit_depth,
        channels=audio_format.channels,
    )


def _counted_sample_rate(output_format: AudioFormat) -> int:
    """
    Return the sample rate a member playing a format counts with for the session rate.

    :param output_format: The format the member plays.
    """
    if output_format.content_type in (ContentType.OPUS, ContentType.MP3, ContentType.AAC):
        return min(output_format.sample_rate, _LOSSY_MAX_SAMPLE_RATE)
    return output_format.sample_rate
