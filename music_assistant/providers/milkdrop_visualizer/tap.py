"""
Audio tap for the MilkDrop visualizer: waveform frames straight from playback.

Reads the decoded PCM that the streams controller already buffers for the item
a player is playing, so any player produces a waveform whatever protocol it
renders over. The read is passive - it takes no audio away from playback and
changes nothing about grouping or output protocol - so watching the visualizer
never interrupts what is playing.

Frames carry a play-at timestamp in the relay's clock domain. The tap anchors
the track's media timeline to that clock from the queue's reported position,
so a viewer draws each frame around the time it is audible. How closely that
matches depends on how precisely the player reports its position.
"""

from __future__ import annotations

import asyncio
import dataclasses
import struct
import time
from collections import deque
from typing import TYPE_CHECKING, cast

import numpy as np
from music_assistant_models.enums import PlaybackState
from music_assistant_models.media_items import MediaItemPalette
from orjson import dumps

from music_assistant.controllers.streams.audio_analysis import SMART_FADES_ANALYSIS_DOMAIN
from music_assistant.controllers.streams.audio_buffer import AudioBufferDiscarded, AudioBufferEOF

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.controllers.streams.audio_buffer import AudioBuffer
    from music_assistant.models.audio_analysis import AudioAnalysisData
    from music_assistant.models.player import Player

    from .provider import MilkdropVisualizerProvider

WAVE_SAMPLES = 1024
CONF_COLOR_TINT = "color_tint"
DEFAULT_COLOR_TINT = True
# Derived from the model, so a field added upstream is forwarded automatically.
COLOR_FIELDS = tuple(field.name for field in dataclasses.fields(MediaItemPalette))
# How far ahead of the audible playhead the tap reads. Viewers schedule frames
# by timestamp, so a lead is what lets them draw on time; it costs nothing,
# since this audio is buffered already.
LEAD_SECONDS = 5.0
# Gap between where the anchor says the playhead is and where the queue reports
# it that means the audio moved (a seek) rather than the player simply reporting
# its position coarsely. Well above the ~1s quantization of the coarsest
# reporters (DLNA), whose jitter would otherwise restart the frame flow.
RESYNC_THRESHOLD_SECONDS = 3.0
# Poll interval while there is nothing to read: idle player, or the tap having
# read as far ahead as it may.
IDLE_POLL_SECONDS = 0.5
# The neural beat tracker lands ~5-10s into a track, so a track that has beats
# at all rarely has them at its first frame. Capped so a track that will never
# have them stops asking.
BEAT_RETRY_SECONDS = 3.0
BEAT_RETRY_ATTEMPTS = 30
# Frames a tap keeps to replay to a viewer that attaches mid-track, and the
# ceiling on one viewer's outbound queue. A frame is ~1KB and they come at
# ~43/s, so these are ~0.5MB and ~1MB per tap at worst: the tap never reads
# more than LEAD_SECONDS ahead, so holding more than the lead buys nothing,
# and a small install (a Pi with a couple of viewers) pays little for it.
RING_FRAMES = 512
VIEWER_QUEUE_FRAMES = 1024

# Wire tags, matching the format documented in relay.py.
WAVE_FRAME_TAG = 22
BEAT_FRAME_TAG = 17


def server_now_us() -> int:
    """Return the relay's clock in microseconds, the domain frame timestamps live in."""
    # Monotonic: a viewer only ever needs the server's clock to be consistent
    # with itself, and a wall clock stepping under NTP would strand every frame
    # already scheduled.
    return int(time.monotonic() * 1_000_000)


def pack_wave_frame(timestamp_us: int, samples: bytes) -> bytes:
    """Pack one waveform tail for the wire."""
    return struct.pack(">Bq", WAVE_FRAME_TAG, timestamp_us) + samples


def pack_beat_frame(timestamp_us: int, is_downbeat: bool) -> bytes:
    """Pack one beat schedule entry for the wire."""
    return struct.pack(">BqB", BEAT_FRAME_TAG, timestamp_us, 1 if is_downbeat else 0)


def pcm_to_mono(data: bytes, pcm_format: AudioFormat) -> np.ndarray:
    """
    Return a PCM chunk as mono float32 in -1.0..1.0.

    :param data: Raw interleaved PCM as the playback buffer holds it.
    :param pcm_format: The buffer's PCM format, giving bit depth and channel count.
    """
    bit_depth = pcm_format.bit_depth
    if bit_depth == 24:
        # No numpy dtype covers packed 24-bit, so assemble the samples by byte.
        packed = np.frombuffer(data, dtype=np.uint8)
        packed = packed[: packed.size - packed.size % 3].reshape(-1, 3).astype(np.int32)
        raw = packed[:, 0] | packed[:, 1] << 8 | packed[:, 2] << 16
        raw[raw >= 1 << 23] -= 1 << 24
        scale = float(1 << 23)
    else:
        dtype = "<i2" if bit_depth == 16 else "<i4"
        width = np.dtype(dtype).itemsize
        raw = np.frombuffer(data[: len(data) - len(data) % width], dtype=dtype)
        scale = float(1 << (width * 8 - 1))
    channels = max(1, pcm_format.channels)
    mono: np.ndarray
    if channels > 1:
        # Fold to mono in one pass, straight to float32: converting the whole
        # interleaved chunk first would cost twice the memory for no gain.
        # A truncated chunk drops its dangling frame rather than failing here.
        raw = raw[: raw.size - raw.size % channels]
        mono = raw.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    else:
        mono = raw.astype(np.float32)
    mono /= scale
    return mono


def palette_payload(palette: MediaItemPalette | None) -> dict[str, list[int] | None]:
    """
    Return a color@v1 payload for a track palette.

    A track without a palette yields every field as null, so a viewer drops the
    previous track's tint rather than keeping it over the new one.

    :param palette: The palette resolved for the artwork now showing, if any.
    """
    payload: dict[str, list[int] | None] = {}
    for name in COLOR_FIELDS:
        value = getattr(palette, name, None) if palette is not None else None
        payload[name] = list(value) if value else None
    return payload


class ViewerQueue:
    """
    Outbound queue for one viewer.

    Bounded so a stalled browser cannot stall the tap, but control messages
    (stream/clear, stream/end) are never dropped: losing one would leave the
    viewer animating stale audio after a seek or track change.
    """

    def __init__(self, capacity: int = VIEWER_QUEUE_FRAMES) -> None:
        """
        Initialize the queue.

        :param capacity: Maximum number of pending items before eviction kicks in.
        """
        self._items: deque[bytes | str] = deque()
        self._capacity = capacity
        self._wakeup = asyncio.Event()

    def push(self, item: bytes | str) -> None:
        """Enqueue an item, evicting the oldest waveform frame when full."""
        if len(self._items) >= self._capacity:
            for index, queued in enumerate(self._items):
                if isinstance(queued, bytes):
                    del self._items[index]
                    break
            else:
                self._items.popleft()
        self._items.append(item)
        self._wakeup.set()

    async def get(self) -> bytes | str:
        """Wait for and return the next item."""
        while not self._items:
            self._wakeup.clear()
            await self._wakeup.wait()
        return self._items.popleft()


@dataclasses.dataclass
class TrackCursor:
    """Where a tap has read to in the current track, and how its media time maps to the clock."""

    item_id: str
    # Clock time at which this track's media time zero was (or will be) audible.
    anchor_us: int
    # Next 1-second buffer chunk to read; chunk N is second N of the track.
    next_chunk: int
    # Samples left over from the previous chunk, and the media time they start at.
    carry: np.ndarray
    carry_media: float

    def playhead(self) -> float:
        """Return where the anchor says the audible playhead is now, in media seconds."""
        return (server_now_us() - self.anchor_us) / 1_000_000


class Tap:
    """One reader of a player's audio, shared by every viewer watching that player."""

    def __init__(self, player_id: str) -> None:
        """
        Initialize the tap.

        :param player_id: The player whose audio this tap follows.
        """
        self.player_id = player_id
        self.queues: set[ViewerQueue] = set()
        # Beat frames with their scheduled timestamps, so viewers that attach
        # mid-track still receive the rest of the track's downbeats.
        self.beats: deque[tuple[int, bytes]] = deque(maxlen=4096)
        # Recent packed waveform frames, replayed to a connecting viewer so it
        # has something to draw before the tap reaches its next chunk.
        self.ring: deque[bytes] = deque(maxlen=RING_FRAMES)
        # Latest color@v1 fields, replayed to viewers that attach mid-track.
        self.last_color: dict[str, list[int] | None] = {}
        # Beat analysis already fetched for the current item, so a re-anchor
        # (a seek) rebuilds the schedule without querying again. Positive only:
        # a cached miss would suppress analysis that lands late in the track.
        self.beats_analysis: tuple[str, AudioAnalysisData] | None = None
        self.task: asyncio.Task[None] | None = None
        self.beats_task: asyncio.Task[None] | None = None

    def fan_out(self, frame: bytes | str) -> None:
        """Deliver a packed frame to every attached viewer queue."""
        for queue in self.queues:
            queue.push(frame)

    def apply_color(self, payload: dict[str, list[int] | None]) -> None:
        """Cache a color@v1 payload and fan it out."""
        self.last_color = payload
        self.fan_out(dumps({"type": "color", "payload": payload}).decode())

    def reset(self, message: str) -> None:
        """Drop everything scheduled from a timeline that no longer applies."""
        self.ring.clear()
        self.beats.clear()
        self.fan_out(message)


class TapManager:
    """Creates, shares and tears down the audio taps."""

    def __init__(self, provider: MilkdropVisualizerProvider) -> None:
        """
        Initialize the tap manager.

        :param provider: The loaded MilkDrop visualizer provider instance.
        """
        self.mass = provider.mass
        self.provider = provider
        self.logger = provider.logger.getChild("tap")
        # One shared tap per player id, refcounted by viewer queues.
        self._taps: dict[str, Tap] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, player: Player) -> Tap:
        """
        Return the shared tap for a player, creating it on first use.

        :param player: The player whose audio to follow.
        """
        async with self._lock:
            if (existing := self._taps.get(player.player_id)) is not None:
                return existing
            tap = Tap(player.player_id)
            self._taps[player.player_id] = tap
            tap.task = self.mass.create_task(self._run(tap))
            self.logger.info("Waveform tap following %s", player.display_name)
            return tap

    def schedule_release(self, player_id: str) -> None:
        """
        Tear down a tap whose last viewer just left.

        Keyed per player with abort_existing, so a player never accumulates
        releases: an earlier one could otherwise tear down a tap that a later
        viewer created.

        :param player_id: The player whose tap may now be idle.
        """
        self.mass.create_task(
            self._release(player_id),
            task_id=f"milkdrop_release_{player_id}",
            abort_existing=True,
        )

    async def close(self) -> None:
        """Tear down every live tap."""
        async with self._lock:
            for tap in self._taps.values():
                self._stop(tap)
            self._taps.clear()

    def pending_beat_frames(self, tap: Tap) -> list[bytes]:
        """
        Return the tap's beat frames that are still in the future.

        :param tap: The tap whose beat schedule to filter.
        """
        now_us = server_now_us()
        return [frame for timestamp_us, frame in tap.beats if timestamp_us > now_us]

    async def _release(self, player_id: str) -> None:
        """Stop and forget a tap as soon as its last viewer goes."""
        async with self._lock:
            tap = self._taps.get(player_id)
            if tap is None or tap.queues:
                return
            self._taps.pop(player_id, None)
            self._stop(tap)
            self.logger.info("Waveform tap for %s removed (viewers gone)", player_id)

    def _stop(self, tap: Tap) -> None:
        """Cancel a tap's reader and anything still working for it."""
        for task in (tap.task, tap.beats_task):
            if task is not None:
                task.cancel()
        tap.task = None
        tap.beats_task = None

    async def _run(self, tap: Tap) -> None:
        """Read the player's audio for as long as the tap lives, packing frames for its viewers."""
        cursor: TrackCursor | None = None
        while True:
            try:
                cursor = await self._read_once(tap, cursor)
            except Exception as err:
                # a source that failed mid-track is a playback problem, not a
                # reason for this tap to stop following the player
                self.logger.debug("Tap for %s could not read: %s", tap.player_id, err)
                cursor = None
                await asyncio.sleep(IDLE_POLL_SECONDS)

    async def _read_once(self, tap: Tap, cursor: TrackCursor | None) -> TrackCursor | None:
        """
        Advance a tap by at most one buffer chunk.

        :param tap: The tap being fed.
        :param cursor: The cursor from the previous pass, if it still has one.
        :return: The cursor to carry into the next pass, or None to start over.
        """
        source = self._playing_source(tap.player_id)
        if source is None:
            if cursor is not None:
                tap.reset('{"type": "stream/end"}')
            await asyncio.sleep(IDLE_POLL_SECONDS)
            return None
        queue, item, buffer = source
        self._sync_color(tap)
        cursor = self._align(tap, cursor, item, queue.corrected_elapsed_time, buffer)
        # Stay ahead of the listener, but never behind the buffer's retained
        # window: a rolling (radio) buffer discards as playback consumes it, and
        # what it is about to drop is the last chance to read that audio.
        if (
            cursor.next_chunk > cursor.playhead() + LEAD_SECONDS
            and cursor.next_chunk > buffer.first_buffered_chunk
        ):
            await asyncio.sleep(IDLE_POLL_SECONDS)
            return cursor
        try:
            pcm = await buffer.read_chunk_for_analysis(cursor.next_chunk)
        except AudioBufferEOF:
            # read past the end of a track that is still finishing; the next
            # item takes over as soon as the queue moves on
            await asyncio.sleep(IDLE_POLL_SECONDS)
            return cursor
        except AudioBufferDiscarded:
            # the retained window moved past us (a stalled tap, or a rolling
            # buffer outrunning it); pick the timeline up again where it is now
            await asyncio.sleep(IDLE_POLL_SECONDS)
            return None
        self._emit_chunk(tap, cursor, pcm, buffer.pcm_format)
        return cursor

    def _playing_source(self, player_id: str) -> tuple[PlayerQueue, QueueItem, AudioBuffer] | None:
        """Return the queue, item and PCM buffer of what a player is playing right now."""
        queue = self.mass.player_queues.get_active_queue(player_id)
        if queue is None or queue.state != PlaybackState.PLAYING:
            return None
        item = queue.current_item
        if item is None or item.streamdetails is None:
            return None
        # An external source (a provider streaming straight to the device) has
        # no buffer here, so there is no audio for us to read.
        buffer = cast("AudioBuffer | None", item.streamdetails.buffer)
        if buffer is None:
            return None
        return queue, item, buffer

    def _align(
        self,
        tap: Tap,
        cursor: TrackCursor | None,
        item: QueueItem,
        playhead: float,
        buffer: AudioBuffer,
    ) -> TrackCursor:
        """
        Return a cursor whose timeline still matches what the player is playing.

        A new track, a seek, or a resume re-anchors the media timeline to the
        relay clock; so does falling behind the buffer's retained window.

        :param tap: The tap being fed.
        :param cursor: The cursor in use, if the tap already has one.
        :param item: The queue item now playing.
        :param playhead: Media position the queue reports for it, in seconds.
        :param buffer: The item's PCM buffer.
        """
        oldest = buffer.first_buffered_chunk
        if (
            cursor is not None
            and cursor.item_id == item.queue_item_id
            and cursor.next_chunk >= oldest
            and abs(cursor.playhead() - playhead) <= RESYNC_THRESHOLD_SECONDS
        ):
            return cursor
        start_chunk = max(int(max(0.0, playhead)), oldest)
        cursor = TrackCursor(
            item_id=item.queue_item_id,
            anchor_us=server_now_us() - int(playhead * 1_000_000),
            next_chunk=start_chunk,
            carry=np.zeros(0, dtype=np.float32),
            carry_media=float(start_chunk),
        )
        tap.reset('{"type": "stream/clear"}')
        self._schedule_beats(tap, item, cursor.anchor_us)
        return cursor

    def _emit_chunk(
        self, tap: Tap, cursor: TrackCursor, pcm: bytes, pcm_format: AudioFormat
    ) -> None:
        """Turn one second of PCM into packed waveform frames and fan them out."""
        sample_rate = pcm_format.sample_rate
        mono = pcm_to_mono(pcm, pcm_format)
        chunk_media = float(cursor.next_chunk)
        if abs(cursor.carry_media + cursor.carry.size / sample_rate - chunk_media) > 0.001:
            # the leftover belongs to audio we are no longer continuing from
            cursor.carry = np.zeros(0, dtype=np.float32)
            cursor.carry_media = chunk_media
        mono = np.concatenate([cursor.carry, mono])
        offset = 0
        while mono.size - offset >= WAVE_SAMPLES:
            window = mono[offset : offset + WAVE_SAMPLES]
            offset += WAVE_SAMPLES
            quantized = np.rint(np.clip(window, -1.0, 1.0) * 127.0 + 128.0).astype(np.uint8)
            # stamped at the end of the window, the instant it finishes sounding
            end_media = cursor.carry_media + offset / sample_rate
            frame = pack_wave_frame(
                cursor.anchor_us + int(end_media * 1_000_000), quantized.tobytes()
            )
            tap.ring.append(frame)
            tap.fan_out(frame)
        cursor.carry = mono[offset:].copy()
        cursor.carry_media += offset / sample_rate
        cursor.next_chunk += 1

    def _sync_color(self, tap: Tap) -> None:
        """Fan out the track palette whenever it changes (once per track, in practice)."""
        if not self.provider.config.get_value(CONF_COLOR_TINT):
            return
        player = self.mass.players.get_player(tap.player_id)
        media = player.state.current_media if player is not None else None
        payload = palette_payload(media.palette if media is not None else None)
        if payload != tap.last_color:
            tap.apply_color(payload)

    def _schedule_beats(self, tap: Tap, item: QueueItem, anchor_us: int) -> None:
        """
        (Re)build a tap's beat schedule for a track.

        A re-anchor of an item whose analysis is already cached (a seek)
        rebuilds the schedule in place, without a task or a new query.

        :param tap: The tap to fan the beats out to.
        :param item: The queue item now playing.
        :param anchor_us: Clock time of that item's media time zero.
        """
        if tap.beats_analysis is not None and tap.beats_analysis[0] == item.queue_item_id:
            self._fan_out_beats(tap, tap.beats_analysis[1], anchor_us)
            return
        tap.beats_task = self.mass.create_task(
            self._hydrate_beats(tap, item, anchor_us),
            task_id=f"milkdrop_beats_{tap.player_id}",
            abort_existing=True,
        )

    async def _hydrate_beats(self, tap: Tap, item: QueueItem, anchor_us: int) -> None:
        """Wait for a track's beat analysis and schedule the beats that are still ahead."""
        streamdetails = item.streamdetails
        if streamdetails is None:
            return
        # smart_fades is the only AA provider that produces beats. Without it,
        # no amount of waiting will turn any up.
        if not self.mass.streams.audio_analysis.smart_fades_provider_available:
            return
        for _ in range(BEAT_RETRY_ATTEMPTS):
            analysis = await self.mass.streams.audio_analysis.get_audio_analysis(
                streamdetails.item_id,
                streamdetails.provider,
                media_type=streamdetails.media_type,
                priority=(SMART_FADES_ANALYSIS_DOMAIN,),
            )
            if analysis is not None and analysis.beats:
                break
            await asyncio.sleep(BEAT_RETRY_SECONDS)
        else:
            self.logger.debug("No beat analysis for %s", streamdetails.uri)
            return
        tap.beats_analysis = (item.queue_item_id, analysis)
        self._fan_out_beats(tap, analysis, anchor_us)

    def _fan_out_beats(self, tap: Tap, analysis: AudioAnalysisData, anchor_us: int) -> None:
        """
        Schedule the analysis beats that are still ahead and fan them out.

        :param tap: The tap to fan the beats out to.
        :param analysis: The beat analysis of the item now playing.
        :param anchor_us: Clock time of that item's media time zero.
        """
        beats = analysis.beats or ()
        downbeats = {float(value) for value in analysis.downbeats or ()}
        now_us = server_now_us()
        scheduled = 0
        for beat in beats:
            timestamp_us = anchor_us + int(float(beat) * 1_000_000)
            if timestamp_us <= now_us:
                continue
            frame = pack_beat_frame(timestamp_us, float(beat) in downbeats)
            tap.beats.append((timestamp_us, frame))
            tap.fan_out(frame)
            scheduled += 1
        self.logger.debug("Scheduled %s of %s beat(s)", scheduled, len(beats))
