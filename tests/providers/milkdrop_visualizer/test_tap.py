"""Tests for the MilkDrop visualizer audio tap."""

from __future__ import annotations

import struct
from unittest.mock import AsyncMock, Mock

import numpy as np
from music_assistant_models.media_items import AudioFormat, MediaItemPalette

from music_assistant.models.audio_analysis import AudioAnalysisData
from music_assistant.providers.milkdrop_visualizer.tap import (
    WAVE_SAMPLES,
    Tap,
    TapManager,
    TrackCursor,
    ViewerQueue,
    pack_wave_frame,
    palette_payload,
    pcm_to_mono,
    server_now_us,
)

PCM_FORMAT = AudioFormat(sample_rate=44100, bit_depth=16, channels=2)


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
    assert len(tap.ring) == 44100 // WAVE_SAMPLES
    assert cursor.carry.size == 44100 % WAVE_SAMPLES
    assert cursor.next_chunk == 1


def test_frame_is_stamped_at_the_end_of_its_window() -> None:
    """A frame plays out at the anchor plus the media time its last sample sits at."""
    manager = _manager()
    tap = Tap("player-1")
    cursor = _cursor(next_chunk=10, anchor_us=1_000_000)
    manager._emit_chunk(tap, cursor, _stereo_pcm([0] * WAVE_SAMPLES), PCM_FORMAT)
    tag, timestamp_us = struct.unpack(">Bq", tap.ring[0][:9])
    assert tag == 22
    assert len(tap.ring[0]) == 9 + WAVE_SAMPLES
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
    assert len(tap.ring) == (2 * 44100) // WAVE_SAMPLES
    _, timestamp_us = struct.unpack(">Bq", tap.ring[-1][:9])
    assert timestamp_us == int(len(tap.ring) * WAVE_SAMPLES / 44100 * 1_000_000)


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
    assert set(tap.ring[0][9:]) == {0x80}


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


def test_ring_with_only_future_frames_is_reported_stale() -> None:
    """A ring whose oldest frame is ahead of now has nothing a fresh viewer can draw."""
    tap = Tap("player-1")
    assert not tap.has_only_future_frames()
    tap.ring.append(pack_wave_frame(server_now_us() - 1_000_000, b"\x80" * WAVE_SAMPLES))
    assert not tap.has_only_future_frames()
    tap.ring.clear()
    tap.ring.append(pack_wave_frame(server_now_us() + 60_000_000, b"\x80" * WAVE_SAMPLES))
    assert tap.has_only_future_frames()


async def test_read_once_realigns_when_requested() -> None:
    """A requested realign drops a pinned-ahead cursor and restarts at the playhead."""
    manager = _manager()
    manager.provider.config.get_value.return_value = False  # type: ignore[attr-defined]
    tap = Tap("player-1")
    queue = Mock(corrected_elapsed_time=100.0, playback_speed=1.0)
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=0, pcm_format=PCM_FORMAT)
    buffer.read_chunk_for_analysis = AsyncMock(return_value=_stereo_pcm([0] * 44100))
    manager._playing_source = Mock(return_value=(queue, item, buffer))  # type: ignore[method-assign]
    # a cursor pinned at the eviction edge but otherwise in sync with the queue
    pinned = _cursor(next_chunk=200, anchor_us=server_now_us() - 100_000_000)
    tap.realign_requested = True
    cursor = await manager._read_once(tap, pinned)
    assert cursor is not None
    assert cursor is not pinned
    assert cursor.next_chunk == 101
    assert not tap.realign_requested


async def test_read_once_ignores_realign_when_playhead_chunk_is_evicted() -> None:
    """A realign past the eviction edge is dropped: healthy viewers keep their frames."""
    manager = _manager()
    manager.provider.config.get_value.return_value = False  # type: ignore[attr-defined]
    tap = Tap("player-1")
    tap.ring.append(b"frame")
    queue = Mock(corrected_elapsed_time=100.0, playback_speed=1.0)
    item = Mock(queue_item_id="item-1")
    buffer = Mock(first_buffered_chunk=200, pcm_format=PCM_FORMAT)
    buffer.read_chunk_for_analysis = AsyncMock(return_value=_stereo_pcm([0] * 44100))
    manager._playing_source = Mock(return_value=(queue, item, buffer))  # type: ignore[method-assign]
    # a cursor pinned at the eviction edge but otherwise in sync with the queue
    pinned = _cursor(next_chunk=200, anchor_us=server_now_us() - 100_000_000)
    tap.realign_requested = True
    cursor = await manager._read_once(tap, pinned)
    assert cursor is pinned
    assert b"frame" in tap.ring
    assert not tap.realign_requested


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
