"""End-to-end render check: the built chain and its filters actually do their job in ffmpeg."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncGenerator

import numpy as np
import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.streams.smart_fades.fades import (
    SmartCrossFade,
    SmartFade,
    StandardCrossFade,
    _feed_ffmpeg_stdin,
)
from music_assistant.controllers.streams.smart_fades.filters import (
    ECHO_DECAYS,
    ECHO_DECLICK_S,
    EchoOutFilter,
    Filter,
    HighPassSweepFilter,
    StreamingCrossfadeFilter,
)
from music_assistant.helpers.process import AsyncProcess
from music_assistant.models.audio_analysis import AudioAnalysisData

PCM = AudioFormat(content_type=ContentType.PCM_F32LE, sample_rate=44100, bit_depth=32, channels=2)
SR = 44100


def _tone(freq: float, seconds: float, level: float = 0.2) -> np.ndarray:
    """Return a stereo-interleaved sine tone."""
    t = np.arange(int(SR * seconds)) / SR
    mono = (level * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.repeat(mono, 2)


class _OutgoingFilterFade(SmartFade):
    """Runs one outgoing-stream filter through the real mixer, ending in a no-op blend."""

    def __init__(self, audio_filter: Filter, fade_out_samples: int) -> None:
        """
        Initialize the fixed chain.

        :param audio_filter: The filter under test.
        :param fade_out_samples: Length of the outgoing stream in PCM samples.
        """
        super().__init__(logging.getLogger())
        # a nofade blend over the whole outgoing stream against a shorter silent
        # fade-in leaves the mix equal to the filtered outgoing stream, length included
        self.filters = [
            audio_filter,
            StreamingCrossfadeFilter(self.logger, fade_out_samples, fadeout_curve="nofade"),
        ]

    def build(
        self, fade_out_bytes_len: int, fade_in_bytes_len: int, pcm_format: AudioFormat
    ) -> None:
        """Nothing to plan: the chain is fixed at construction."""


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
    analysis.band_rms_low = np.full(1800, low, dtype=np.float32).tolist()
    analysis.band_rms_low_mid = np.full(1800, low_mid, dtype=np.float32).tolist()
    analysis.band_rms_mid = np.full(1800, mid, dtype=np.float32).tolist()
    analysis.band_rms_high = np.full(1800, high, dtype=np.float32).tolist()
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


def _window(x: np.ndarray, start_s: float, end_s: float) -> np.ndarray:
    """Slice an interleaved stereo signal between two times."""
    return x[int(start_s * SR) * 2 : int(end_s * SR) * 2]


def _envelope_peak_time(x: np.ndarray, freq: float, start_s: float, end_s: float) -> float:
    """Time of the loudest moment of one frequency in the left channel within a window."""
    mono = _window(x, start_s, end_s)[0::2].astype(np.float64)
    t = np.arange(len(mono)) / SR
    width = int(0.005 * SR)
    envelope = np.abs(
        np.convolve(mono * np.exp(-2j * np.pi * freq * t), np.ones(width) / width, mode="same")
    )
    return start_s + float(np.argmax(envelope)) / SR


async def _render_outgoing(audio_filter: Filter, fade_out: np.ndarray) -> np.ndarray:
    """Run an outgoing-stream filter through the real mixer and return the filtered stream."""
    fade = _OutgoingFilterFade(audio_filter, len(fade_out) // 2)
    silence = np.zeros(SR * 2, dtype=np.float32).tobytes()
    chunks = [chunk async for chunk in fade.apply(fade_out.tobytes(), silence, PCM)]
    return np.frombuffer(b"".join(chunks), dtype=np.float32)


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
async def test_highpass_sweep_takes_the_low_end_out() -> None:
    """The swept high-pass leaves the bass alone before the sweep and removes it after."""
    fade_out = _tone(60.0, 8.0) + _tone(3000.0, 8.0)
    mix = await _render_outgoing(HighPassSweepFilter(logging.getLogger(), 2.0, 5.0), fade_out)
    assert len(mix) == len(fade_out)

    def _level(x: np.ndarray, start_s: float, end_s: float, freq: float) -> float:
        return _band_rms(_window(x, start_s, end_s), freq - 5, freq + 5)

    def _ratio(start_s: float, end_s: float, freq: float) -> float:
        return _level(mix, start_s, end_s, freq) / _level(fade_out, start_s, end_s, freq)

    # 20 Hz before the sweep is transparent for a 60 Hz tone; 600 Hz after it is ~-40 dB
    assert _ratio(0.5, 1.9, 60.0) > 0.98
    assert _ratio(5.5, 7.5, 60.0) < 0.03
    # the cutoff ramps rather than jumps: the bass drops through every part of the sweep
    sweep = [_ratio(start, start + 1.0, 60.0) for start in (2.0, 3.0, 4.0)]
    assert sweep[0] > sweep[1] > sweep[2] > _ratio(5.5, 7.5, 60.0)
    # the top end passes throughout
    assert _ratio(5.5, 7.5, 3000.0) > 0.98


@pytest.mark.asyncio
async def test_echo_out_repeats_the_last_beat_and_cuts_the_dry_signal() -> None:
    """At the cut the dry signal stops and only the beat before it echoes, decaying per tap."""
    cut, beat = 4.0, 0.5
    # a Hann-shaped 50 ms burst on every beat: 1 kHz on the beat before the cut, 2 kHz
    # on all others, so the 1 kHz taps are the echo and any 2 kHz past the cut is dry
    mono = np.zeros(int(8.0 * SR), dtype=np.float32)
    burst = np.hanning(int(0.05 * SR))
    t = np.arange(len(burst)) / SR
    for index in range(16):
        freq = 1000.0 if index == round((cut - beat) / beat) else 2000.0
        start = int(index * beat * SR)
        mono[start : start + len(burst)] = 0.3 * burst * np.sin(2 * np.pi * freq * t)
    fade_out = np.repeat(mono, 2)
    mix = await _render_outgoing(EchoOutFilter(logging.getLogger(), cut, beat), fade_out)

    assert len(mix) == len(fade_out)
    np.testing.assert_allclose(_window(mix, 0.0, cut), _window(fade_out, 0.0, cut), atol=1e-6)
    # nothing dry past the de-click, and no 2 kHz beat from before the slice echoes
    dry_after = _band_rms(_window(mix, cut + ECHO_DECLICK_S, 8.0), 1950, 2050)
    assert dry_after < 0.01 * _band_rms(_window(fade_out, cut + ECHO_DECLICK_S, 8.0), 1950, 2050)
    # one tap per decay at cut + k * beat, each at its decay relative to the source beat
    source = _band_rms(_window(fade_out, cut - beat, cut), 950, 1050)
    for k, decay in enumerate(ECHO_DECAYS):
        tap_start = cut + k * beat
        peak = _envelope_peak_time(mix, 1000.0, tap_start - 0.1, tap_start + 0.3)
        assert peak == pytest.approx(tap_start + 0.025, abs=0.002)
        tap = _band_rms(_window(mix, tap_start, tap_start + beat), 950, 1050)
        assert tap / source == pytest.approx(decay, rel=0.03)
    # silence once the last tap has played out
    assert np.max(np.abs(_window(mix, cut + len(ECHO_DECAYS) * beat, 8.0))) < 1e-6


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


@pytest.mark.asyncio
async def test_cancelled_feed_does_not_hang_writing_eof() -> None:
    """A feeder cancelled while blocked on a full stdin pipe must return, not stall on the EOF."""
    proc = AsyncProcess(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=True,
        name="stdin-blackhole",
    )
    await proc.start()

    async def _endless_zeros() -> AsyncGenerator[bytes]:
        chunk = b"\x00" * (1024 * 1024)
        while True:
            yield chunk

    try:
        feed_task = asyncio.create_task(_feed_ffmpeg_stdin(proc, _endless_zeros()))
        # let the feeder fill the pipe and the transport's write buffer, then block
        await asyncio.sleep(0.5)
        assert not feed_task.done()
        feed_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(feed_task, timeout=2)
    finally:
        # the child never reads its stdin, so a graceful close would only wait out its timeout
        await proc.kill()


@pytest.mark.asyncio
async def test_source_cancelled_feed_still_ends_the_input() -> None:
    """A fade-in that raises CancelledError itself still ends the mixer's input with an EOF."""
    # the child exits only once its stdin reaches EOF
    proc = AsyncProcess(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        stdin=True,
        name="stdin-reader",
    )
    await proc.start()

    async def _cancelled_fade_in() -> AsyncGenerator[bytes]:
        yield b"\x00" * 1024
        raise asyncio.CancelledError

    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(_feed_ffmpeg_stdin(proc, _cancelled_fade_in()))
        assert await proc.wait_with_timeout(2) == 0
    finally:
        await proc.close()
