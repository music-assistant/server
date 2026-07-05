"""End-to-end render check: the built chain actually swaps the bass in ffmpeg."""

from __future__ import annotations

import logging

import numpy as np
import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.smart_fades.fades import SmartCrossFade
from music_assistant.models.audio_analysis import AudioAnalysisData

PCM = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2)
SR = 44100


def _tone(freq: float, seconds: float, level: float = 0.2) -> np.ndarray:
    """Return a stereo-interleaved sine tone."""
    t = np.arange(int(SR * seconds)) / SR
    mono = (level * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.repeat(mono, 2)


def _analysis(bpm: float, duration: float) -> AudioAnalysisData:
    """Synthetic flat-energy analysis with a steady beat grid."""
    interval = 60.0 / bpm
    beats = np.arange(0.0, duration, interval, dtype=np.float32)
    return AudioAnalysisData(
        duration=duration,
        bpm=bpm,
        beats=beats,
        downbeats=beats[::4],
        rms_energy=np.full(1800, 0.5, dtype=np.float32),
        key="A",
        mode="minor",
    )


def _band_rms(x: np.ndarray, lo: float, hi: float) -> float:
    """RMS of one frequency band of the (interleaved stereo) signal's left channel."""
    mono = x[0::2]
    spec = np.abs(np.fft.rfft(mono))
    freqs = np.fft.rfftfreq(len(mono), 1 / SR)
    mask = (freqs >= lo) & (freqs < hi)
    return float(np.sqrt(np.mean(spec[mask] ** 2)))


@pytest.mark.asyncio
async def test_bass_swaps_between_tracks() -> None:
    """Early mix output carries A's bass (60Hz); late output carries B's (90Hz)."""
    fade = SmartCrossFade(logging.getLogger(), _analysis(120.0, 240.0), _analysis(120.0, 240.0))
    fade_out = (_tone(60.0, 45.0) + _tone(3000.0, 45.0)).tobytes()  # A: 60Hz bass
    fade_in = (_tone(90.0, 45.0) + _tone(5000.0, 45.0)).tobytes()  # B: 90Hz bass
    fade.build(len(fade_out), len(fade_in), PCM)
    chunks = [chunk async for chunk in fade.apply(fade_out, fade_in, PCM)]
    mix = np.frombuffer(b"".join(chunks), dtype=np.float32)
    third = len(mix) // 3 // 2 * 2
    head, tail = mix[:third], mix[-third:]
    # A's bass dominates early; B's bass dominates late (the swap happened)
    assert _band_rms(head, 55, 65) > 3 * _band_rms(head, 85, 95)
    assert _band_rms(tail, 85, 95) > 3 * _band_rms(tail, 55, 65)
