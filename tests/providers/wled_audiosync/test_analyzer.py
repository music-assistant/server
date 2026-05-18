"""
Unit tests for ``WledAudioAnalyzer.process_frame``.

The analyzer is a pure-function transformation (modulo three small pieces
of running state) from ``aiosendspin.models.visualizer.VisualizerFrame``
to ``WledV2Frame``. These tests exercise:

- spectrum padding / truncation invariants,
- loudness → sample_raw scaling,
- exponential smoothing in sample_smth,
- global-AGC normalisation across all 16 fft_bands,
- f_peak pass-through,
- the rolling beat-detection in sample_peak,
- the None-on-missing-spectrum guard.
"""

from __future__ import annotations

from aiosendspin.models.visualizer import VisualizerFrame

from music_assistant.providers.wled_audiosync.wled_audiosync_bridge import (
    WLED_FFT_BANDS,
    WledAudioAnalyzer,
)


def _frame(
    *,
    timestamp_us: int = 0,
    loudness: int = 0,
    f_peak: int = 0,
    spectrum: list[int] | None = None,
) -> VisualizerFrame:
    """Build a VisualizerFrame with explicit, named keyword args."""
    return VisualizerFrame(
        timestamp_us=timestamp_us,
        loudness=loudness,
        f_peak=f_peak,
        spectrum=list(spectrum) if spectrum is not None else None,
    )


def test_missing_spectrum_returns_none() -> None:
    """A VisualizerFrame with spectrum=None yields no WledV2Frame to emit."""
    analyzer = WledAudioAnalyzer()
    assert analyzer.process_frame(_frame(spectrum=None)) is None


def test_silent_frame_produces_zero_wled_frame() -> None:
    """All-zero inputs yield zero bands, zero sample_raw, zero magnitude, zero peak."""
    analyzer = WledAudioAnalyzer()
    frame = _frame(spectrum=[0] * WLED_FFT_BANDS)
    out = analyzer.process_frame(frame)
    assert out is not None
    assert out.fft_bands == bytes(WLED_FFT_BANDS)
    assert out.sample_raw == 0.0
    assert out.sample_smth == 0.0
    assert out.fft_magnitude == 0.0
    assert out.fft_major_peak_hz == 0.0
    assert out.sample_peak == 0


def test_short_spectrum_is_padded_to_16_bands() -> None:
    """A spectrum shorter than 16 bins is zero-padded out to 16 fft_bands bytes."""
    analyzer = WledAudioAnalyzer()
    short_spec = [50, 100, 200]
    out = analyzer.process_frame(_frame(spectrum=short_spec))
    assert out is not None
    assert len(out.fft_bands) == WLED_FFT_BANDS
    # The first three bands carry the non-zero data; the rest are zero.
    assert all(b == 0 for b in out.fft_bands[3:])


def test_long_spectrum_is_truncated_to_16_bands() -> None:
    """A spectrum longer than 16 bins is truncated to the first 16 bands."""
    analyzer = WledAudioAnalyzer()
    long_spec = list(range(20))  # bins 0..19
    out = analyzer.process_frame(_frame(spectrum=long_spec))
    assert out is not None
    assert len(out.fft_bands) == WLED_FFT_BANDS


def test_loudness_max_maps_sample_raw_to_full_scale() -> None:
    """Loudness == 65535 (uint16 max) maps to sample_raw == 255."""
    analyzer = WledAudioAnalyzer()
    out = analyzer.process_frame(_frame(loudness=65535, spectrum=[0] * WLED_FFT_BANDS))
    assert out is not None
    assert out.sample_raw == 255.0


def test_loudness_half_scale_maps_sample_raw_proportionally() -> None:
    """Half-max loudness yields half-scale sample_raw (~127.5)."""
    analyzer = WledAudioAnalyzer()
    out = analyzer.process_frame(_frame(loudness=32_767, spectrum=[0] * WLED_FFT_BANDS))
    assert out is not None
    # 32767 / 65535 * 255 ≈ 127.498
    assert 127.0 < out.sample_raw < 128.0


def test_f_peak_passes_through_to_fft_major_peak_hz() -> None:
    """The Sendspin f_peak field is copied verbatim into FFT_MajorPeak."""
    analyzer = WledAudioAnalyzer()
    out = analyzer.process_frame(_frame(f_peak=1_234, spectrum=[0] * WLED_FFT_BANDS))
    assert out is not None
    assert out.fft_major_peak_hz == 1_234.0


def test_sample_smth_converges_to_step_input() -> None:
    """Exponential smoothing over a step input approaches the step value."""
    analyzer = WledAudioAnalyzer()
    target_loudness = 50_000
    spectrum = [0] * WLED_FFT_BANDS
    smths: list[float] = []
    for _ in range(30):
        out = analyzer.process_frame(_frame(loudness=target_loudness, spectrum=spectrum))
        assert out is not None
        smths.append(out.sample_smth)
    expected_target = target_loudness / 65_535.0 * 255.0
    # Monotonic non-decreasing toward the target.
    assert smths[0] < smths[5] < smths[-1]
    # After 30 frames at alpha=0.3, smth should be within ~1% of target.
    assert abs(smths[-1] - expected_target) < expected_target * 0.01


def test_global_agc_normalises_bright_band_to_full_scale() -> None:
    """A single bright spectrum bin is normalised toward full scale on the wire."""
    analyzer = WledAudioAnalyzer()
    spectrum = [0] * WLED_FFT_BANDS
    spectrum[5] = 1_000
    # First frame: AGC envelope picks up the new peak.
    out = analyzer.process_frame(_frame(spectrum=spectrum))
    assert out is not None
    bands = list(out.fft_bands)
    # Band 5 is the only non-zero band, and it maps to 255 (denom == 1000).
    assert bands[5] == 255
    assert all(b == 0 for i, b in enumerate(bands) if i != 5)


def test_global_agc_keeps_quieter_bands_proportional() -> None:
    """Bands quieter than the loudest one keep proportional intensity, not all-255."""
    analyzer = WledAudioAnalyzer()
    spectrum = [0] * WLED_FFT_BANDS
    spectrum[5] = 200
    spectrum[8] = 100
    out = analyzer.process_frame(_frame(spectrum=spectrum))
    assert out is not None
    bands = list(out.fft_bands)
    # Band 5 should be ~255, band 8 should be ~127 (half), others zero.
    assert bands[5] == 255
    assert 120 < bands[8] < 135, bands[8]
    # Bands with no input are zero.
    assert bands[0] == 0
    assert bands[15] == 0


def test_agc_decays_so_quieter_followup_frames_reveal_content() -> None:
    """After a loud frame, the AGC envelope releases so quieter frames still show content."""
    analyzer = WledAudioAnalyzer(agc_release_frames=5)
    loud = [0] * WLED_FFT_BANDS
    loud[5] = 1_000
    analyzer.process_frame(_frame(spectrum=loud))
    # Now feed a much quieter spectrum many times; envelope should release.
    quiet = [0] * WLED_FFT_BANDS
    quiet[5] = 100
    last_band5 = 0
    for _ in range(30):
        out = analyzer.process_frame(_frame(spectrum=quiet))
        assert out is not None
        last_band5 = out.fft_bands[5]
    # After 30 release frames, band 5 should be back at (near) full scale —
    # not stuck at the 25/255 ratio left by the loud frame's envelope.
    assert last_band5 > 200, last_band5


def test_fft_magnitude_tracks_loudest_spectrum_bin() -> None:
    """FFT_Magnitude is a proxy for max(spectrum) — receivers treat it as relative."""
    analyzer = WledAudioAnalyzer()
    spectrum = [0] * WLED_FFT_BANDS
    spectrum[3] = 800
    out = analyzer.process_frame(_frame(spectrum=spectrum))
    assert out is not None
    assert out.fft_magnitude == 800.0


def test_sample_peak_requires_warmup_history() -> None:
    """sample_peak stays 0 until rolling history accumulates a minimum window."""
    analyzer = WledAudioAnalyzer()
    spectrum = [0] * WLED_FFT_BANDS
    # First few frames — history not deep enough for beat detection.
    for _ in range(3):
        out = analyzer.process_frame(_frame(loudness=10_000, spectrum=spectrum))
        assert out is not None
        assert out.sample_peak == 0


def test_sample_peak_flags_sudden_loudness_spike() -> None:
    """After warm-up, a loudness spike well above the rolling mean flags samplePeak=1."""
    analyzer = WledAudioAnalyzer()
    spectrum = [0] * WLED_FFT_BANDS
    # Warm up with steady low loudness.
    for _ in range(16):
        analyzer.process_frame(_frame(loudness=5_000, spectrum=spectrum))
    # Now a big spike.
    out = analyzer.process_frame(_frame(loudness=60_000, spectrum=spectrum))
    assert out is not None
    assert out.sample_peak == 1


def test_sample_peak_stays_zero_on_steady_signal() -> None:
    """Steady-state loudness without spikes does not flag a beat."""
    analyzer = WledAudioAnalyzer()
    spectrum = [0] * WLED_FFT_BANDS
    # 20 frames of identical loudness.
    last_peak = 0
    for _ in range(20):
        out = analyzer.process_frame(_frame(loudness=20_000, spectrum=spectrum))
        assert out is not None
        last_peak = out.sample_peak
    # Identical samples → zero variance → no spike → no peak.
    assert last_peak == 0


def test_none_loudness_treated_as_zero() -> None:
    """A VisualizerFrame with loudness=None must not crash; sample_raw is zero."""
    analyzer = WledAudioAnalyzer()
    out = analyzer.process_frame(
        VisualizerFrame(timestamp_us=0, loudness=None, f_peak=440, spectrum=[0] * WLED_FFT_BANDS)
    )
    assert out is not None
    assert out.sample_raw == 0.0


def test_none_f_peak_treated_as_zero_hz() -> None:
    """A VisualizerFrame with f_peak=None yields FFT_MajorPeak=0.0."""
    analyzer = WledAudioAnalyzer()
    out = analyzer.process_frame(
        VisualizerFrame(timestamp_us=0, loudness=10_000, f_peak=None, spectrum=[0] * WLED_FFT_BANDS)
    )
    assert out is not None
    assert out.fft_major_peak_hz == 0.0
