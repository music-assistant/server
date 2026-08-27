"""End-to-end render check: the built chain actually swaps the bass in ffmpeg."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

import numpy as np
import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.smart_fades.fades import SmartCrossFade, StandardCrossFade
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
        beats=beats.tolist(),
        downbeats=beats[::4].tolist(),
        rms_energy=np.full(1800, 0.5, dtype=np.float32).tolist(),
        key="A",
        mode="minor",
    )


def _with_bands(
    analysis: AudioAnalysisData, low: float, low_mid: float, mid: float, high: float
) -> AudioAnalysisData:
    """Attach flat ``band_rms`` envelopes at the given amplitudes."""
    analysis.extra_data = {
        "band_rms": {
            "low": np.full(1800, low, dtype=np.float32).tolist(),
            "low_mid": np.full(1800, low_mid, dtype=np.float32).tolist(),
            "mid": np.full(1800, mid, dtype=np.float32).tolist(),
            "high": np.full(1800, high, dtype=np.float32).tolist(),
        }
    }
    return analysis


def _analysis_with_mid_bands(bpm: float, duration: float) -> AudioAnalysisData:
    """Analysis with a mid-heavy, bass-light ``band_rms`` profile that clears the mid gate."""
    # bass-light so the low swap stays out of the way; mid-heavy and constant
    # so duty_mid saturates to 1.0 and F_mid clears the 0.18-0.30 gate corridor
    return _with_bands(_analysis(bpm, duration), 0.05, 0.3, 0.7, 0.3)


def _analysis_with_instrumental_bands(bpm: float, duration: float) -> AudioAnalysisData:
    """Analysis with a bass-light, mid-light profile: every measured EQ gate bypasses."""
    # f_low ~0.014 and f_mid ~0.13 sit below their gate corridors, so both the
    # low and mid swap bypass while anchors/entry stay on the full-band paths
    return _with_bands(_analysis(bpm, duration), 0.1, 0.55, 0.3, 0.55)


def _band_rms(x: np.ndarray, lo: float, hi: float) -> float:
    """RMS of one frequency band of the (interleaved stereo) signal's left channel."""
    mono = x[0::2]
    spec = np.abs(np.fft.rfft(mono))
    freqs = np.fft.rfftfreq(len(mono), 1 / SR)
    mask = (freqs >= lo) & (freqs < hi)
    return float(np.sqrt(np.mean(spec[mask] ** 2)))


async def _render(
    out_analysis: AudioAnalysisData,
    in_analysis: AudioAnalysisData,
    fade_out: bytes,
    fade_in: bytes,
) -> tuple[np.ndarray, SmartCrossFade]:
    """Build and apply a SmartCrossFade, returning the rendered mix and the fade."""
    fade = SmartCrossFade(logging.getLogger(), out_analysis, in_analysis)
    fade.build(len(fade_out), len(fade_in), PCM)
    chunks = [chunk async for chunk in fade.apply(fade_out, fade_in, PCM)]
    return np.frombuffer(b"".join(chunks), dtype=np.float32), fade


def _cf_slice(mix: np.ndarray, fade: SmartCrossFade, frac0: float, frac1: float) -> np.ndarray:
    """Slice the rendered crossfade window between two fractions of its span."""
    timing = fade.timing_info
    start_s = timing.pre_crossfade_duration + frac0 * timing.crossfade_duration
    end_s = timing.pre_crossfade_duration + frac1 * timing.crossfade_duration
    return mix[int(start_s * SR) * 2 : int(end_s * SR) * 2]


@pytest.mark.asyncio
async def test_bass_swaps_between_tracks() -> None:
    """The low shelves attenuate A's bass and duck B's entrance vs an EQ-bypassed render."""
    fade_out = (_tone(60.0, 45.0) + _tone(3000.0, 45.0)).tobytes()  # A: 60Hz bass
    fade_in = (_tone(90.0, 45.0) + _tone(5000.0, 45.0)).tobytes()  # B: 90Hz bass
    # differential render: identical PCM, one plan with the shipped full-depth
    # kill (no band data) and one whose measured gates bypass all low shelves --
    # any energy difference is then attributable to the low EQ, not acrossfade
    killed_mix, killed = await _render(
        _analysis(120.0, 240.0), _analysis(120.0, 240.0), fade_out, fade_in
    )
    open_mix, open_ = await _render(
        _analysis_with_instrumental_bands(120.0, 240.0),
        _analysis_with_instrumental_bands(120.0, 240.0),
        fade_out,
        fade_in,
    )
    assert killed.plan is not None
    assert killed.plan.eq_plan.low_out is not None
    assert open_.plan is not None
    assert open_.plan.eq_plan.low_out is None
    assert open_.plan.eq_plan.low_in is None
    # identical geometry: the band data must only change EQ, never the timing
    assert len(killed_mix) == len(open_mix)
    # measure inside the crossfade window itself: A's bass is killed where the
    # swap completes (late); B enters bass-ducked (early); -26dB kill leaves
    # well under 30% of the bypassed render's energy
    killed_late = _cf_slice(killed_mix, killed, 0.7, 0.95)
    open_late = _cf_slice(open_mix, open_, 0.7, 0.95)
    killed_early = _cf_slice(killed_mix, killed, 0.05, 0.3)
    open_early = _cf_slice(open_mix, open_, 0.05, 0.3)
    assert _band_rms(killed_late, 55, 65) < 0.3 * _band_rms(open_late, 55, 65)
    assert _band_rms(killed_early, 85, 95) < 0.3 * _band_rms(open_early, 85, 95)
    # sanity on the killed render alone: A's bass dominates early, B's late
    assert _band_rms(killed_early, 55, 65) > 3 * _band_rms(killed_early, 85, 95)
    assert _band_rms(killed_late, 85, 95) > 3 * _band_rms(killed_late, 55, 65)


@pytest.mark.asyncio
async def test_mid_swaps_between_tracks() -> None:
    """The mid peaks trade A's 1kHz for B's 2kHz vs an EQ-bypassed render of the same PCM."""
    fade_out = _tone(1000.0, 45.0).tobytes()  # A: 1kHz "vocal"
    fade_in = _tone(2000.0, 45.0).tobytes()  # B: 2kHz "vocal"
    # differential render: identical PCM, one plan whose band data engages the
    # mid gate and one whose band data bypasses every measured EQ gate -- the
    # 1k/2k energy difference is then attributable to the mid EQ alone
    gated_mix, gated = await _render(
        _analysis_with_mid_bands(120.0, 240.0),
        _analysis_with_mid_bands(120.0, 240.0),
        fade_out,
        fade_in,
    )
    open_mix, open_ = await _render(
        _analysis_with_instrumental_bands(120.0, 240.0),
        _analysis_with_instrumental_bands(120.0, 240.0),
        fade_out,
        fade_in,
    )
    assert gated.plan is not None
    assert gated.plan.eq_plan.mid_out is not None
    assert gated.plan.eq_plan.mid_in is not None
    assert open_.plan is not None
    assert open_.plan.eq_plan.mid_out is None
    assert open_.plan.eq_plan.mid_in is None
    # identical geometry: the band data must only change EQ, never the timing
    assert len(gated_mix) == len(open_mix)
    # the -8dB depth is modest, so assert a measurable drop (not dominance):
    # A's 1kHz is attenuated where the swap completes (late); B's 2kHz enters
    # ducked (early); both measured against the EQ-bypassed render, inside
    # the crossfade window itself
    gated_late = _cf_slice(gated_mix, gated, 0.7, 0.95)
    open_late = _cf_slice(open_mix, open_, 0.7, 0.95)
    gated_early = _cf_slice(gated_mix, gated, 0.05, 0.3)
    open_early = _cf_slice(open_mix, open_, 0.05, 0.3)
    assert _band_rms(gated_late, 950, 1050) < 0.7 * _band_rms(open_late, 950, 1050)
    assert _band_rms(gated_early, 1950, 2050) < 0.7 * _band_rms(open_early, 1950, 2050)


@pytest.mark.asyncio
async def test_a_failing_fade_in_ends_the_mix_instead_of_hanging() -> None:
    """An incoming stream that dies mid-overlap must not leave ffmpeg waiting for input."""
    fade_out = _tone(220.0, 6.0).tobytes()
    delivered = _tone(440.0, 1.0).tobytes()

    async def _dying_fade_in() -> AsyncGenerator[bytes]:
        yield delivered
        raise RuntimeError("incoming source died")

    fade = StandardCrossFade(logging.getLogger(), crossfade_duration=2)
    fade.build(len(fade_out), len(_tone(440.0, 4.0).tobytes()), PCM)

    async def _drain_mix() -> None:
        # the timeout only bounds the failure: without the EOF the mix hangs here
        async with asyncio.timeout(30):
            async for _chunk in fade.apply(fade_out, _dying_fade_in(), PCM):
                pass

    started = asyncio.get_event_loop().time()
    with pytest.raises(RuntimeError, match="incoming source died"):
        await _drain_mix()
    assert asyncio.get_event_loop().time() - started < 10
