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
