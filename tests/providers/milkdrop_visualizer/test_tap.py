"""Tests for the MilkDrop visualizer audio tap."""

from __future__ import annotations

import struct
from unittest.mock import Mock

from aiosendspin.models.color import SessionUpdateColor
from aiosendspin.models.types import UndefinedField
from aiosendspin.server.roles import AudioChunk
from orjson import loads

from music_assistant.providers.milkdrop_visualizer.tap import (
    WAVE_SAMPLES,
    MilkdropWaveRole,
    Tap,
    ViewerQueue,
    _extract_color_update,
)


def _stereo_chunk(mono_values: list[int], *, dangling_sample: bool = False) -> AudioChunk:
    """Build an AudioChunk from mono int16 values, duplicated across L/R."""
    samples: list[int] = []
    for value in mono_values:
        samples.extend((value, value))
    if dangling_sample:
        # One unpaired int16 (even byte count, odd sample count): the reshape
        # would fail without the role's drop-the-dangling-sample guard.
        samples.append(0)
    data = struct.pack(f"<{len(samples)}h", *samples)
    return AudioChunk(data=data, timestamp_us=1_000, duration_us=21_000, byte_count=len(data))


def _role() -> tuple[MilkdropWaveRole, list[tuple[int, bytes]]]:
    """Return a wave role wired to a capturing callback."""
    role = MilkdropWaveRole(client=Mock(client_id="milkdrop-test"))
    captured: list[tuple[int, bytes]] = []
    role.set_wave_callback(lambda ts, samples: captured.append((ts, samples)))
    return role, captured


def test_emits_one_tail_per_full_buffer() -> None:
    """A chunk with at least WAVE_SAMPLES mono samples emits a single 1024-byte tail."""
    role, captured = _role()
    role.on_audio_chunk(_stereo_chunk([0] * WAVE_SAMPLES))
    assert len(captured) == 1
    ts_us, samples = captured[0]
    assert len(samples) == WAVE_SAMPLES
    # timestamp is the chunk end (start + duration)
    assert ts_us == 1_000 + 21_000


def test_no_emit_before_warmup() -> None:
    """A short chunk buffers silently until enough samples have accumulated."""
    role, captured = _role()
    role.on_audio_chunk(_stereo_chunk([0] * (WAVE_SAMPLES // 2)))
    assert captured == []
    role.on_audio_chunk(_stereo_chunk([0] * (WAVE_SAMPLES // 2)))
    assert len(captured) == 1


def test_quantization_is_symmetric_offset_binary() -> None:
    """Full-scale +/- and zero map to 255 / 1 / 128, never wrapping."""
    role, captured = _role()
    role.on_audio_chunk(_stereo_chunk([32767] * WAVE_SAMPLES))
    role.on_audio_chunk(_stereo_chunk([-32768] * WAVE_SAMPLES))
    role.on_audio_chunk(_stereo_chunk([0] * WAVE_SAMPLES))
    assert set(captured[0][1]) == {255}
    assert set(captured[1][1]) == {1}
    assert set(captured[2][1]) == {128}


def test_odd_length_chunk_does_not_raise() -> None:
    """A truncated (odd int16 count) chunk drops the dangling sample instead of failing."""
    role, captured = _role()
    role.on_audio_chunk(_stereo_chunk([0] * WAVE_SAMPLES, dangling_sample=True))
    # Still produced a tail from the surviving even-length buffer.
    assert len(captured) == 1
    assert len(captured[0][1]) == WAVE_SAMPLES


def test_stream_start_resets_the_rolling_buffer() -> None:
    """A stream boundary clears buffered samples so a partial tail never carries over."""
    role, captured = _role()
    role.on_audio_chunk(_stereo_chunk([0] * (WAVE_SAMPLES - 1)))
    role.on_stream_start()
    role.on_audio_chunk(_stereo_chunk([0] * 1))
    # Without the reset the two chunks would have summed past the threshold.
    assert captured == []


def test_viewer_queue_evicts_oldest_waveform_not_control() -> None:
    """When full, the queue drops the oldest binary frame and keeps control messages."""
    queue = ViewerQueue(capacity=3)
    queue.push(b"wave-1")
    queue.push('{"type": "stream/clear"}')
    queue.push(b"wave-2")
    # Full now; the next push must evict the oldest *binary* frame (wave-1).
    queue.push(b"wave-3")
    drained = [queue._items[i] for i in range(len(queue._items))]
    assert b"wave-1" not in drained
    assert '{"type": "stream/clear"}' in drained
    assert b"wave-2" in drained
    assert b"wave-3" in drained


def test_viewer_queue_evicts_control_only_when_no_binary_left() -> None:
    """With nothing but control messages queued, eviction falls back to the oldest item."""
    queue = ViewerQueue(capacity=2)
    queue.push('{"type": "stream/clear"}')
    queue.push('{"type": "stream/end"}')
    queue.push('{"type": "stream/start"}')
    drained = [queue._items[i] for i in range(len(queue._items))]
    assert len(drained) == 2
    assert '{"type": "stream/start"}' in drained


def _color_payload(**fields: tuple[int, int, int] | None) -> Mock:
    """Build a ServerStatePayload-shaped mock whose color carries only the given fields."""
    color = Mock(spec=SessionUpdateColor)
    for name in (
        "background_dark",
        "background_light",
        "primary",
        "accent",
        "on_dark",
        "on_light",
    ):
        setattr(color, name, fields.get(name, UndefinedField()))
    return Mock(color=color)


def test_extract_color_update_skips_undefined_fields() -> None:
    """Only fields the server actually touched make it into the update."""
    payload = _color_payload(primary=(10, 20, 30))
    assert _extract_color_update(payload) == {"primary": (10, 20, 30)}


def test_extract_color_update_keeps_explicit_nulls() -> None:
    """A field explicitly cleared to None is forwarded, not dropped like an undefined one."""
    payload = _color_payload(primary=None)
    assert _extract_color_update(payload) == {"primary": None}


def test_extract_color_update_returns_empty_when_payload_has_no_color() -> None:
    """A server/state message that doesn't touch color yields nothing to forward."""
    assert _extract_color_update(Mock(color=None)) == {}


def test_tap_apply_color_merges_into_cached_palette() -> None:
    """Successive partial updates accumulate rather than overwrite the whole palette."""
    tap = Tap("milkdrop-test")
    tap.apply_color({"primary": (10, 20, 30)})
    tap.apply_color({"accent": (1, 2, 3)})
    assert tap.last_color == {"primary": (10, 20, 30), "accent": (1, 2, 3)}


def test_tap_apply_color_fans_out_only_the_update() -> None:
    """Viewers receive just what changed, not the whole accumulated palette."""
    tap = Tap("milkdrop-test")
    queue = ViewerQueue()
    tap.queues.add(queue)
    tap.apply_color({"primary": (10, 20, 30)})
    tap.apply_color({"accent": (1, 2, 3)})
    first = loads(queue._items[0])
    second = loads(queue._items[1])
    assert first == {"type": "color", "payload": {"primary": [10, 20, 30]}}
    assert second == {"type": "color", "payload": {"accent": [1, 2, 3]}}


def test_tap_apply_color_ignores_an_empty_update() -> None:
    """An update with nothing new (e.g. an undefined-only payload) fans out nothing."""
    tap = Tap("milkdrop-test")
    queue = ViewerQueue()
    tap.queues.add(queue)
    tap.apply_color({})
    assert not queue._items
