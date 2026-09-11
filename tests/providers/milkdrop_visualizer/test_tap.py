"""Tests for the MilkDrop visualizer audio tap."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, Mock

import numpy as np
from music_assistant_models.media_items import AudioFormat, MediaItemPalette

from music_assistant.controllers.streams.audio_buffer import AudioBufferDiscarded, AudioBufferEOF
from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.providers.milkdrop_visualizer.tap import (
    PENDING_FRAMES,
    RING_PAST_SECONDS,
    WAVE_SAMPLES,
    Tap,
    TapManager,
    TrackCursor,
    ViewerQueue,
    pack_wave_frame,
    palette_payload,
    pcm_to_mono,
    server_now_us,
    wave_frame_timestamp,
)

PCM_FORMAT = AudioFormat(sample_rate=44100, bit_depth=16, channels=2)


def _pending_frames(tap: Tap) -> list[bytes]:
    """Return the packed frames currently held in a tap's pending queue."""
    return [frame for _, frame in tap.pending]


def _stereo_pcm(mono_values: list[int], *, dangling_sample: bool = False) -> bytes:
    """Build interleaved 16-bit stereo PCM from mono values, duplicated across L/R."""
    samples: list[int] = []
    for value in mono_values:
        samples.extend((value, value))
    if dangling_sample:
        # One unpaired sample: the reshape would fail without the drop guard.
        samples.append(0)
    return struct.pack(f"<{len(samples)}h", *samples)


def _manager() -> TapManager:
    """Return a tap manager whose provider is inert."""
    provider = Mock()
    provider.logger.getChild.return_value = Mock()
    manager = TapManager(provider)
    manager._schedule_beats = Mock()  # type: ignore[method-assign]
    return manager


def _cursor(next_chunk: int = 0, anchor_us: int = 0) -> TrackCursor:
    """Return a cursor positioned at the start of a chunk."""
    return TrackCursor(
        item_id="item-1",
        anchor_us=anchor_us,
        next_chunk=next_chunk,
        carry=np.zeros(0, dtype=np.float32),
        carry_media=float(next_chunk),
    )


def test_mono_fold_averages_channels() -> None:
    """A stereo chunk folds to one sample per frame, scaled into -1.0..1.0."""
    mono = pcm_to_mono(_stereo_pcm([0, 16384, -16384]), PCM_FORMAT)
    assert mono.size == 3
    assert mono[0] == 0.0
    assert round(float(mono[1]), 3) == 0.5
    assert round(float(mono[2]), 3) == -0.5


def test_mono_fold_drops_a_dangling_sample() -> None:
    """A truncated chunk loses its unpaired sample instead of failing the reshape."""
    assert pcm_to_mono(_stereo_pcm([0, 0], dangling_sample=True), PCM_FORMAT).size == 2


def test_mono_fold_reads_packed_24_bit() -> None:
    """24-bit PCM has no numpy dtype, so its sign handling is worth pinning down."""
    fmt = AudioFormat(sample_rate=44100, bit_depth=24, channels=1)
    # 0, +full scale - 1, -full scale
    data = b"\x00\x00\x00" + b"\xff\xff\x7f" + b"\x00\x00\x80"
    mono = pcm_to_mono(data, fmt)
    assert mono[0] == 0.0
    assert round(float(mono[1]), 3) == 1.0
    assert round(float(mono[2]), 3) == -1.0


def test_emits_one_frame_per_1024_samples() -> None:
    """A one-second chunk yields a frame per full window, keeping the remainder back."""
    manager = _manager()
    tap = Tap("player-1")
    cursor = _cursor()
    manager._emit_chunk(tap, cursor, _stereo_pcm([0] * 44100), PCM_FORMAT)
    assert len(tap.pending) == 44100 // WAVE_SAMPLES
    assert cursor.carry.size == 44100 % WAVE_SAMPLES
    assert cursor.next_chunk == 1


def test_frame_is_stamped_at_the_end_of_its_window() -> None:
    """A frame plays out at the anchor plus the media time its last sample sits at."""
    manager = _manager()
    tap = Tap("player-1")
    cursor = _cursor(next_chunk=10, anchor_us=1_000_000)
    manager._emit_chunk(tap, cursor, _stereo_pcm([0] * WAVE_SAMPLES), PCM_FORMAT)
    frame = _pending_frames(tap)[0]
    tag, timestamp_us = struct.unpack(">Bq", frame[:9])
    assert tag == 22
    assert len(frame) == 9 + WAVE_SAMPLES
    # chunk 10 is media second 10, plus one 1024-sample window
    expected_media = 10 + WAVE_SAMPLES / 44100
    assert timestamp_us == 1_000_000 + int(expected_media * 1_000_000)


def test_carry_continues_into_the_next_chunk() -> None:
    """Samples left over from a chunk complete the first window of the next one."""
    manager = _manager()
    tap = Tap("player-1")
    cursor = _cursor()
    second = _stereo_pcm([0] * 44100)
    manager._emit_chunk(tap, cursor, second, PCM_FORMAT)
    manager._emit_chunk(tap, cursor, second, PCM_FORMAT)
    # windows tile the two seconds end to end, rather than restarting per chunk
    frames = _pending_frames(tap)
    assert len(frames) == (2 * 44100) // WAVE_SAMPLES
    _, timestamp_us = struct.unpack(">Bq", frames[-1][:9])
    assert timestamp_us == int(len(frames) * WAVE_SAMPLES / 44100 * 1_000_000)


def test_carry_is_dropped_when_the_next_chunk_is_elsewhere() -> None:
    """After a resync the leftover belongs to audio we are no longer continuing from."""
    manager = _manager()
    tap = Tap("player-1")
    cursor = _cursor()
    manager._emit_chunk(tap, cursor, _stereo_pcm([0] * 44100), PCM_FORMAT)
    carried = cursor.carry.size
    assert carried
    cursor.next_chunk = 60
    manager._emit_chunk(tap, cursor, _stereo_pcm([0] * 44100), PCM_FORMAT)
    assert cursor.carry_media > 60


def test_quantized_samples_are_offset_binary() -> None:
    """Silence sits at 0x80, so a viewer reads the tail without knowing the scale."""
    manager = _manager()
    tap = Tap("player-1")
    manager._emit_chunk(tap, _cursor(), _stereo_pcm([0] * WAVE_SAMPLES), PCM_FORMAT)
    assert set(_pending_frames(tap)[0][9:]) == {0x80}


def test_align_keeps_a_cursor_that_still_matches() -> None:
    """A cursor on the same track, in step with the queue, is left alone."""
    manager = _manager()
    tap = Tap("player-1")
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=0)
    cursor = manager._align(tap, None, item, 5.0, buffer)
    assert manager._align(tap, cursor, item, 5.0, buffer) is cursor


def test_align_re_anchors_on_a_track_change() -> None:
    """A new queue item drops what was scheduled from the old track's timeline."""
    manager = _manager()
    tap = Tap("player-1")
    buffer = Mock(first_buffered_chunk=0)
    cursor = manager._align(tap, None, Mock(queue_item_id="item-1"), 30.0, buffer)
    tap.ring.append(b"stale")
    queued = ViewerQueue()
    tap.queues.add(queued)
    new_cursor = manager._align(tap, cursor, Mock(queue_item_id="item-2"), 0.0, buffer)
    assert new_cursor is not cursor
    assert new_cursor.next_chunk == 0
    assert not tap.ring
    assert queued._items[0] == '{"type": "stream/clear"}'


def test_align_re_anchors_on_a_seek() -> None:
    """A playhead that jumps away from the anchored timeline restarts the cursor."""
    manager = _manager()
    tap = Tap("player-1")
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=0)
    cursor = manager._align(tap, None, item, 5.0, buffer)
    new_cursor = manager._align(tap, cursor, item, 120.0, buffer)
    assert new_cursor is not cursor
    assert new_cursor.next_chunk == 120


def test_align_starts_inside_the_retained_window() -> None:
    """A rolling buffer that has discarded the playhead is picked up where it starts."""
    manager = _manager()
    tap = Tap("player-1")
    item = Mock(queue_item_id="item-1")
    cursor = manager._align(tap, None, item, 5.0, Mock(first_buffered_chunk=90))
    assert cursor.next_chunk == 90


def test_align_catches_up_without_resetting_when_eviction_overtakes_the_cursor() -> None:
    """A cursor still on the same timeline just catches up to eviction, no reset."""
    manager = _manager()
    tap = Tap("player-1")
    item = Mock(queue_item_id="item-1")
    cursor = manager._align(tap, None, item, 5.0, Mock(first_buffered_chunk=0))
    tap.ring.append(b"kept")
    queued = ViewerQueue()
    tap.queues.add(queued)
    caught_up = manager._align(tap, cursor, item, 5.0, Mock(first_buffered_chunk=50))
    assert caught_up is cursor
    assert caught_up.next_chunk == 50
    assert b"kept" in tap.ring
    assert not queued._items


def test_playhead_tracks_playback_speed() -> None:
    """At 2x, media time advances two seconds per wall-clock second from the anchor."""
    cursor = _cursor(anchor_us=server_now_us() - 10_000_000)
    cursor.speed = 2.0
    assert abs(cursor.playhead() - 20.0) < 0.1
    # and the inverse mapping stamps media second 20 at (roughly) now
    assert abs(cursor.media_to_clock_us(20.0) - server_now_us()) < 100_000


def test_align_keeps_a_speed_aware_cursor_in_step() -> None:
    """A cursor anchored at 2x stays matched against a queue advancing in media-time."""
    manager = _manager()
    tap = Tap("player-1")
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=0)
    cursor = manager._align(tap, None, item, 10.0, buffer, 2.0)
    assert cursor.speed == 2.0
    assert manager._align(tap, cursor, item, 10.0, buffer, 2.0) is cursor


def test_align_scales_the_resync_threshold_by_speed() -> None:
    """At 2x, report jitter inflates by the speed factor, so the threshold grows with it."""
    manager = _manager()
    tap = Tap("player-1")
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=0)
    cursor = manager._align(tap, None, item, 10.0, buffer, 2.0)
    # a 5s media-time gap is within the scaled 6s threshold, not a seek
    assert manager._align(tap, cursor, item, 15.0, buffer, 2.0) is cursor
    # beyond the scaled threshold it is a seek and re-anchors
    assert manager._align(tap, cursor, item, 17.0, buffer, 2.0) is not cursor


def test_align_re_anchors_on_a_speed_change() -> None:
    """A playback speed change remaps media time to the clock, so the cursor restarts."""
    manager = _manager()
    tap = Tap("player-1")
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=0)
    cursor = manager._align(tap, None, item, 10.0, buffer)
    new_cursor = manager._align(tap, cursor, item, 10.0, buffer, 1.5)
    assert new_cursor is not cursor
    assert new_cursor.speed == 1.5


def _beats_manager() -> TapManager:
    """Return a tap manager with the real beat scheduling in place."""
    provider = Mock()
    provider.logger.getChild.return_value = Mock()
    return TapManager(provider)


def test_schedule_beats_rebuilds_from_cached_analysis() -> None:
    """A re-anchor of an item whose analysis is cached reschedules in place, without a task."""
    manager = _beats_manager()
    tap = Tap("player-1")
    tap.beats_analysis = ("item-1", AudioAnalysisData(beats=[1.0, 2.0], downbeats=[1.0]))
    anchor_us = server_now_us()
    manager._schedule_beats(tap, Mock(queue_item_id="item-1"), anchor_us)
    manager.mass.create_task.assert_not_called()  # type: ignore[attr-defined]
    assert [timestamp_us for timestamp_us, _ in tap.beats] == [
        anchor_us + 1_000_000,
        anchor_us + 2_000_000,
    ]
    # the downbeat flag survives the rebuild
    assert tap.beats[0][1][9] == 1
    assert tap.beats[1][1][9] == 0


def test_fan_out_beats_scales_media_time_by_speed() -> None:
    """At 2x a beat at media second 2 sounds one wall-clock second after the anchor."""
    manager = _beats_manager()
    tap = Tap("player-1")
    anchor_us = server_now_us()
    manager._fan_out_beats(tap, AudioAnalysisData(beats=[2.0]), anchor_us, 2.0)
    assert [timestamp_us for timestamp_us, _ in tap.beats] == [anchor_us + 1_000_000]


def test_reset_cancels_an_in_flight_beat_hydration() -> None:
    """A timeline reset stops a pending hydration from landing beats for a dead track."""
    tap = Tap("player-1")
    task = Mock()
    tap.beats_task = task
    tap.reset('{"type": "stream/end"}')
    task.cancel.assert_called_once()
    assert tap.beats_task is None


def test_schedule_beats_does_not_serve_another_item_from_cache() -> None:
    """A cached analysis belongs to one item; any other item hydrates freshly."""
    manager = _beats_manager()
    tap = Tap("player-1")
    tap.beats_analysis = ("item-1", AudioAnalysisData(beats=[1.0]))
    manager._schedule_beats(tap, Mock(queue_item_id="item-2"), server_now_us())
    manager.mass.create_task.assert_called_once()  # type: ignore[attr-defined]
    assert not tap.beats


async def test_hydrate_beats_caches_the_fetched_analysis() -> None:
    """The fetched analysis is kept on the tap so the next re-anchor skips the query."""
    manager = _beats_manager()
    analysis = AudioAnalysisData(beats=[1.0])
    manager.mass.streams.audio_analysis.get_audio_analysis = AsyncMock(  # type: ignore[method-assign]
        return_value=analysis
    )
    tap = Tap("player-1")
    await manager._hydrate_beats(tap, Mock(queue_item_id="item-1"), server_now_us())
    assert tap.beats_analysis == ("item-1", analysis)
    assert len(tap.beats) == 1


def test_release_due_releases_only_frames_within_lead_of_now() -> None:
    """Only a frame within LEAD_SECONDS of now moves to the ring; the rest stays pending."""
    manager = _manager()
    tap = Tap("player-1")
    queued = ViewerQueue()
    tap.queues.add(queued)
    now_us = server_now_us()
    near = pack_wave_frame(now_us + 1_000_000, b"\x80" * WAVE_SAMPLES)
    far = pack_wave_frame(now_us + 60_000_000, b"\x80" * WAVE_SAMPLES)
    tap.pending.append((now_us + 1_000_000, near))
    tap.pending.append((now_us + 60_000_000, far))
    manager._release_due(tap)
    assert list(tap.ring) == [near]
    assert _pending_frames(tap) == [far]
    assert list(queued._items) == [near]


def test_release_due_keeps_a_time_window_whatever_the_frame_rate() -> None:
    """A high frame rate is bounded by time in the ring, not by a frame count."""
    manager = _manager()
    tap = Tap("player-1")
    now_us = server_now_us()
    step_us = 5_000  # 200 frames/s, well above a typical wave rate
    start_us = now_us - 3_000_000
    end_us = now_us + 4_000_000  # still within LEAD_SECONDS(5s), so all are due
    timestamp_us = start_us
    while timestamp_us <= end_us:
        frame = pack_wave_frame(timestamp_us, b"\x80" * WAVE_SAMPLES)
        tap.pending.append((timestamp_us, frame))
        timestamp_us += step_us
    manager._release_due(tap)
    assert not tap.pending
    oldest_allowed_us = now_us - int(RING_PAST_SECONDS * 1_000_000) - 5_000
    assert all(wave_frame_timestamp(frame) >= oldest_allowed_us for frame in tap.ring)
    assert wave_frame_timestamp(tap.ring[-1]) == end_us
    assert len(tap.ring) > 512


def test_release_due_drops_frames_older_than_the_past_window() -> None:
    """A ring frame older than RING_PAST_SECONDS is trimmed even with nothing new to release."""
    manager = _manager()
    tap = Tap("player-1")
    now_us = server_now_us()
    old = pack_wave_frame(now_us - 5_000_000, b"\x80" * WAVE_SAMPLES)
    recent = pack_wave_frame(now_us - 500_000, b"\x80" * WAVE_SAMPLES)
    tap.ring.append(old)
    tap.ring.append(recent)
    manager._release_due(tap)
    assert list(tap.ring) == [recent]


def test_reset_clears_pending() -> None:
    """A timeline reset also drops frames held back for later release."""
    tap = Tap("player-1")
    tap.pending.append((0, b"frame"))
    tap.reset('{"type": "stream/end"}')
    assert not tap.pending


async def test_read_once_reads_whatever_is_buffered() -> None:
    """A cursor far ahead of the playhead still reads as long as the buffer already has it."""
    manager = _manager()
    manager.provider.config.get_value.return_value = False  # type: ignore[attr-defined]
    tap = Tap("player-1")
    queue = Mock(corrected_elapsed_time=100.0, playback_speed=1.0)
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=100, seconds_available=60, pcm_format=PCM_FORMAT)
    buffer.read_chunk_for_analysis = AsyncMock(return_value=_stereo_pcm([0] * 44100))
    manager._playing_source = Mock(return_value=(queue, item, buffer))  # type: ignore[method-assign]
    # far beyond the release lead, but still inside the retained window (100-160)
    pinned = _cursor(next_chunk=150, anchor_us=server_now_us() - 100_000_000)
    cursor = await manager._read_once(tap, pinned)
    assert cursor is pinned
    buffer.read_chunk_for_analysis.assert_awaited_once_with(150)
    assert cursor.next_chunk == 151


async def test_read_once_does_not_read_past_what_the_buffer_has_produced() -> None:
    """A cursor caught up to the buffer's produced edge waits rather than reading nothing."""
    manager = _manager()
    manager.provider.config.get_value.return_value = False  # type: ignore[attr-defined]
    tap = Tap("player-1")
    queue = Mock(corrected_elapsed_time=100.0, playback_speed=1.0)
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=100, seconds_available=60, pcm_format=PCM_FORMAT)
    buffer.read_chunk_for_analysis = AsyncMock(return_value=_stereo_pcm([0] * 44100))
    manager._playing_source = Mock(return_value=(queue, item, buffer))  # type: ignore[method-assign]
    # exactly at first_buffered_chunk + seconds_available: nothing more has been produced yet
    pinned = _cursor(next_chunk=160, anchor_us=server_now_us() - 100_000_000)
    cursor = await manager._read_once(tap, pinned)
    assert cursor is pinned
    buffer.read_chunk_for_analysis.assert_not_awaited()
    assert cursor.next_chunk == 160


async def test_read_once_does_not_read_when_pending_is_full() -> None:
    """A tap already holding PENDING_FRAMES back stops reading instead of growing further."""
    manager = _manager()
    manager.provider.config.get_value.return_value = False  # type: ignore[attr-defined]
    tap = Tap("player-1")
    far_us = server_now_us() + 3600 * 1_000_000
    tap.pending.extend((far_us, b"frame") for _ in range(PENDING_FRAMES))
    queue = Mock(corrected_elapsed_time=5.0, playback_speed=1.0)
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=0, seconds_available=60, pcm_format=PCM_FORMAT)
    buffer.read_chunk_for_analysis = AsyncMock(return_value=_stereo_pcm([0] * 44100))
    manager._playing_source = Mock(return_value=(queue, item, buffer))  # type: ignore[method-assign]
    # a matching cursor, so _align does not reset the tap (and its pending queue) under us
    cursor_in = _cursor(next_chunk=5, anchor_us=server_now_us() - 5_000_000)
    await manager._read_once(tap, cursor_in)
    buffer.read_chunk_for_analysis.assert_not_awaited()


async def test_read_once_keeps_the_cursor_on_discarded() -> None:
    """A discarded read leaves the cursor in place for _align to catch up next pass."""
    manager = _manager()
    manager.provider.config.get_value.return_value = False  # type: ignore[attr-defined]
    tap = Tap("player-1")
    queue = Mock(corrected_elapsed_time=5.0, playback_speed=1.0)
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=0, seconds_available=60, pcm_format=PCM_FORMAT)
    buffer.read_chunk_for_analysis = AsyncMock(side_effect=AudioBufferDiscarded)
    manager._playing_source = Mock(return_value=(queue, item, buffer))  # type: ignore[method-assign]
    cursor_in = _cursor(next_chunk=5, anchor_us=server_now_us() - 5_000_000)
    cursor = await manager._read_once(tap, cursor_in)
    assert cursor is cursor_in


async def test_read_once_resumes_on_a_replacement_buffer_after_cancellation() -> None:
    """A cancelled buffer keeps the cursor, and reading carries on once the item has a new one."""
    manager = _manager()
    manager.provider.config.get_value.return_value = False  # type: ignore[attr-defined]
    tap = Tap("player-1")
    queue = Mock(corrected_elapsed_time=5.0, playback_speed=1.0)
    item = Mock(queue_item_id="item-1")
    cancelled = Mock(first_buffered_chunk=0, seconds_available=60, pcm_format=PCM_FORMAT)
    cancelled.read_chunk_for_analysis = AsyncMock(side_effect=AudioBufferDiscarded)
    replacement = Mock(first_buffered_chunk=0, seconds_available=60, pcm_format=PCM_FORMAT)
    replacement.read_chunk_for_analysis = AsyncMock(return_value=_stereo_pcm([0] * 44100))
    manager._playing_source = Mock(  # type: ignore[method-assign]
        side_effect=[(queue, item, cancelled), (queue, item, replacement)]
    )
    cursor_in = _cursor(next_chunk=5, anchor_us=server_now_us() - 5_000_000)
    cursor = await manager._read_once(tap, cursor_in)
    cursor = await manager._read_once(tap, cursor)
    assert cursor is cursor_in
    replacement.read_chunk_for_analysis.assert_awaited_once_with(5)
    assert cursor.next_chunk == 6


async def test_read_once_releases_due_frames_on_eof() -> None:
    """A read that hits EOF still releases any pending frames that came due."""
    manager = _manager()
    manager.provider.config.get_value.return_value = False  # type: ignore[attr-defined]
    tap = Tap("player-1")
    now_us = server_now_us()
    due = pack_wave_frame(now_us, b"\x80" * WAVE_SAMPLES)
    tap.pending.append((now_us, due))
    queue = Mock(corrected_elapsed_time=5.0, playback_speed=1.0)
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=0, seconds_available=60, pcm_format=PCM_FORMAT)
    buffer.read_chunk_for_analysis = AsyncMock(side_effect=AudioBufferEOF)
    manager._playing_source = Mock(return_value=(queue, item, buffer))  # type: ignore[method-assign]
    cursor_in = _cursor(next_chunk=5, anchor_us=server_now_us() - 5_000_000)
    cursor = await manager._read_once(tap, cursor_in)
    assert cursor is cursor_in
    assert due in tap.ring
    assert not tap.pending


def test_palette_payload_maps_every_field() -> None:
    """A palette becomes the color@v1 payload the wire format documents."""
    payload = palette_payload(MediaItemPalette(primary=(1, 2, 3)))
    assert payload["primary"] == [1, 2, 3]
    assert payload["accent"] is None


def test_palette_payload_nulls_everything_without_a_palette() -> None:
    """A track with no palette clears the previous track's tint."""
    payload = palette_payload(None)
    assert payload
    assert all(value is None for value in payload.values())


def test_viewer_queue_evicts_oldest_binary_frame_when_full() -> None:
    """A stalled viewer loses waveform frames rather than stalling the tap."""
    queue = ViewerQueue(capacity=2)
    queue.push(b"first")
    queue.push(b"second")
    queue.push('{"type": "stream/clear"}')
    drained = [queue._items[index] for index in range(len(queue._items))]
    assert drained == [b"second", '{"type": "stream/clear"}']


def test_viewer_queue_evicts_control_only_when_no_binary_left() -> None:
    """Control messages are kept while any waveform frame can be dropped instead."""
    queue = ViewerQueue(capacity=2)
    queue.push('{"type": "stream/start"}')
    queue.push('{"type": "stream/clear"}')
    queue.push(b"frame")
    drained = [queue._items[index] for index in range(len(queue._items))]
    assert len(drained) == 2
    assert '{"type": "stream/clear"}' in drained
