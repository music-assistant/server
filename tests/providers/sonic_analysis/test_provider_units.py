"""Unit tests for the _pcm_bytes_to_audio decoder."""

import struct

import numpy as np
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.sonic_analysis import _pcm_bytes_to_audio


def _af(content_type: ContentType, channels: int = 1) -> AudioFormat:
    """Build a minimal AudioFormat for the PCM decoder tests."""
    return AudioFormat(content_type=content_type, channels=channels)


def _make_pcm_16bit(samples: list[int]) -> bytes:
    """Build raw 16-bit little-endian PCM bytes from integer sample values."""
    return struct.pack(f"<{len(samples)}h", *samples)


def _make_pcm_32bit_int(samples: list[int]) -> bytes:
    """Build raw 32-bit signed little-endian PCM bytes from integer sample values."""
    return struct.pack(f"<{len(samples)}i", *samples)


def _make_pcm_32bit_float(samples: list[float]) -> bytes:
    """Build raw 32-bit float little-endian PCM bytes from float sample values."""
    return struct.pack(f"<{len(samples)}f", *samples)


def test_pcm_16bit_mono() -> None:
    """16-bit mono: max positive sample should convert to ~1.0."""
    pcm = _make_pcm_16bit([0, 16384, -16384, 32767])
    audio = _pcm_bytes_to_audio(_af(ContentType.PCM_S16LE), pcm)
    assert audio.dtype == np.float32
    assert len(audio) == 4
    assert abs(audio[0]) < 1e-6
    assert abs(audio[1] - 0.5) < 0.001
    assert abs(audio[2] + 0.5) < 0.001
    assert abs(audio[3] - 1.0) < 0.001


def test_pcm_16bit_stereo_downmix() -> None:
    """16-bit stereo: two channels should be averaged to mono."""
    pcm = _make_pcm_16bit([32767, 0, 0, 32767])
    audio = _pcm_bytes_to_audio(_af(ContentType.PCM_S16LE, channels=2), pcm)
    assert len(audio) == 2
    assert abs(audio[0] - 0.5) < 0.001
    assert abs(audio[1] - 0.5) < 0.001


def test_pcm_s32_mono() -> None:
    """Signed 32-bit int mono: max positive sample should convert to ~1.0."""
    pcm = _make_pcm_32bit_int([0, 2147483647])
    audio = _pcm_bytes_to_audio(_af(ContentType.PCM_S32LE), pcm)
    assert audio.dtype == np.float32
    assert len(audio) == 2
    assert abs(audio[0]) < 1e-6
    assert abs(audio[1] - 1.0) < 0.01


def test_pcm_f32_mono_round_trips() -> None:
    """Float32 PCM passes through verbatim — the case the bit-depth-only dispatch broke."""
    pcm = _make_pcm_32bit_float([0.0, 0.5, -0.5, 1.0])
    audio = _pcm_bytes_to_audio(_af(ContentType.PCM_F32LE), pcm)
    assert audio.dtype == np.float32
    np.testing.assert_array_almost_equal(audio, np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32))


def test_pcm_24bit_mono() -> None:
    """24-bit mono: verify positive and negative values convert correctly."""
    pos_max = (0x7FFFFF).to_bytes(3, byteorder="little", signed=False)
    zero = (0).to_bytes(3, byteorder="little", signed=False)
    neg_one = (0xFFFFFF).to_bytes(3, byteorder="little", signed=False)
    pcm = zero + pos_max + neg_one
    audio = _pcm_bytes_to_audio(_af(ContentType.PCM_S24LE), pcm)
    assert len(audio) == 3
    assert abs(audio[0]) < 1e-6
    assert abs(audio[1] - 1.0) < 0.001
    assert abs(audio[2] + (1.0 / 8388608.0)) < 0.001


# --------------------------------------------------------------------------- #
#  _get_or_assign_label (tested via instance state dicts)                      #
# --------------------------------------------------------------------------- #


class _FakeLabelMapper:
    """Minimal stand-in that replicates the label mapping logic."""

    def __init__(self) -> None:
        self._label_map: dict[int, tuple[str, str]] = {}
        self._reverse_label_map: dict[tuple[str, str], int] = {}
        self._next_label: int = 1

    def _get_or_assign_label(self, item_id: str, provider: str) -> int:
        key = (item_id, provider)
        if key in self._reverse_label_map:
            return self._reverse_label_map[key]
        label = self._next_label
        self._next_label += 1
        self._label_map[label] = key
        self._reverse_label_map[key] = label
        return label


def test_label_idempotent() -> None:
    """Same (item_id, provider) always returns the same label."""
    m = _FakeLabelMapper()
    label1 = m._get_or_assign_label("track1", "spotify")
    label2 = m._get_or_assign_label("track1", "spotify")
    assert label1 == label2


def test_label_unique_per_pair() -> None:
    """Different (item_id, provider) pairs get different labels."""
    m = _FakeLabelMapper()
    a = m._get_or_assign_label("track1", "spotify")
    b = m._get_or_assign_label("track1", "tidal")
    c = m._get_or_assign_label("track2", "spotify")
    assert len({a, b, c}) == 3


def test_label_maps_bidirectional() -> None:
    """Label map and reverse map are consistent."""
    m = _FakeLabelMapper()
    label = m._get_or_assign_label("track1", "spotify")
    assert m._label_map[label] == ("track1", "spotify")
    assert m._reverse_label_map[("track1", "spotify")] == label


def test_label_starts_at_one() -> None:
    """First assigned label should be 1."""
    m = _FakeLabelMapper()
    assert m._get_or_assign_label("a", "b") == 1


def test_label_increments() -> None:
    """Labels should increment sequentially."""
    m = _FakeLabelMapper()
    labels = [m._get_or_assign_label(f"t{i}", "p") for i in range(5)]
    assert labels == [1, 2, 3, 4, 5]
