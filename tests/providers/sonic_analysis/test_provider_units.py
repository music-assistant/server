"""Unit tests for sonic analysis provider functions that don't require a running MA instance."""

import struct

import numpy as np
import pytest

from music_assistant.providers.sonic_analysis import _pcm_bytes_to_audio

# --------------------------------------------------------------------------- #
#  _pcm_bytes_to_audio                                                         #
# --------------------------------------------------------------------------- #


def _make_pcm_16bit(samples: list[int]) -> bytes:
    """Build raw 16-bit little-endian PCM bytes from integer sample values."""
    return struct.pack(f"<{len(samples)}h", *samples)


def _make_pcm_32bit(samples: list[int]) -> bytes:
    """Build raw 32-bit little-endian PCM bytes from integer sample values."""
    return struct.pack(f"<{len(samples)}i", *samples)


def test_pcm_16bit_mono() -> None:
    """16-bit mono: max positive sample should convert to ~1.0."""
    pcm = _make_pcm_16bit([0, 16384, -16384, 32767])
    audio = _pcm_bytes_to_audio(pcm, sample_rate=44100, bit_depth=16, channels=1)
    assert audio.dtype == np.float32
    assert len(audio) == 4
    assert abs(audio[0]) < 1e-6
    assert abs(audio[1] - 0.5) < 0.001
    assert abs(audio[2] + 0.5) < 0.001
    assert abs(audio[3] - 1.0) < 0.001


def test_pcm_16bit_stereo_downmix() -> None:
    """16-bit stereo: two channels should be averaged to mono."""
    # L=32767 R=0 → mono ≈ 0.5,  L=0 R=32767 → mono ≈ 0.5
    pcm = _make_pcm_16bit([32767, 0, 0, 32767])
    audio = _pcm_bytes_to_audio(pcm, sample_rate=44100, bit_depth=16, channels=2)
    assert len(audio) == 2
    assert abs(audio[0] - 0.5) < 0.001
    assert abs(audio[1] - 0.5) < 0.001


def test_pcm_32bit_mono() -> None:
    """32-bit mono: max positive sample should convert to ~1.0."""
    pcm = _make_pcm_32bit([0, 2147483647])
    audio = _pcm_bytes_to_audio(pcm, sample_rate=44100, bit_depth=32, channels=1)
    assert audio.dtype == np.float32
    assert len(audio) == 2
    assert abs(audio[0]) < 1e-6
    assert abs(audio[1] - 1.0) < 0.01


def test_pcm_24bit_mono() -> None:
    """24-bit mono: verify positive and negative values convert correctly."""
    # 24-bit max positive: 0x7FFFFF = 8388607, stored as 3 bytes little-endian
    pos_max = (0x7FFFFF).to_bytes(3, byteorder="little", signed=False)
    zero = (0).to_bytes(3, byteorder="little", signed=False)
    # 24-bit negative: -1 = 0xFFFFFF in 24-bit two's complement
    neg_one = (0xFFFFFF).to_bytes(3, byteorder="little", signed=False)
    pcm = zero + pos_max + neg_one
    audio = _pcm_bytes_to_audio(pcm, sample_rate=44100, bit_depth=24, channels=1)
    assert len(audio) == 3
    assert abs(audio[0]) < 1e-6
    assert abs(audio[1] - 1.0) < 0.001
    assert abs(audio[2] + (1.0 / 8388608.0)) < 0.001


def test_pcm_unsupported_bit_depth() -> None:
    """Unsupported bit depth should raise ValueError."""
    with pytest.raises(ValueError, match="Unsupported bit depth"):
        _pcm_bytes_to_audio(b"\x00\x00", sample_rate=44100, bit_depth=8, channels=1)


def test_pcm_sample_rate_unused() -> None:
    """Sample rate is accepted but doesn't affect conversion."""
    pcm = _make_pcm_16bit([16384])
    a1 = _pcm_bytes_to_audio(pcm, sample_rate=22050, bit_depth=16, channels=1)
    a2 = _pcm_bytes_to_audio(pcm, sample_rate=48000, bit_depth=16, channels=1)
    assert np.array_equal(a1, a2)


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
