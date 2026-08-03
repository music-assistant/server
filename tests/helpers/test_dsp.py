"""Tests for the DSP helpers."""

import math

from music_assistant_models.dsp import (
    AudioChannel,
    BalanceFilter,
    CompressorFilter,
    ConvolutionFilter,
    CrossfeedFilter,
    GainFilter,
    HighLowPassFilter,
    HighLowPassMode,
    HighLowPassSlope,
    ParametricEQBand,
    ParametricEQBandType,
    ParametricEQFilter,
    SafetyLimiterFilter,
    StereoWidthFilter,
    ToneControlFilter,
    TransposeFilter,
)
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.helpers.dsp import (
    ComplexFilter,
    ComplexFilterInput,
    filter_to_ffmpeg_params,
)

INPUT_FORMAT = AudioFormat(sample_rate=48000)
IR_DIR = "/irs"


def test_gain_filter() -> None:
    """Test that a gain filter maps to a volume filter in dB."""
    dsp_filter = GainFilter(enabled=True, gain=5.5)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == ["volume=5.5dB"]
    dsp_filter = GainFilter(enabled=True, gain=-15.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == ["volume=-15.0dB"]


def test_gain_filter_zero_is_passthrough() -> None:
    """Test that a gain filter with 0 dB gain emits no ffmpeg filter."""
    dsp_filter = GainFilter(enabled=True, gain=0.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == []


def test_balance_filter_right() -> None:
    """Test that balance towards the right attenuates only the left channel."""
    dsp_filter = BalanceFilter(enabled=True, balance=40)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        "pan=stereo|FL=0.6*FL|FR=FR"
    ]


def test_balance_filter_left() -> None:
    """Test that balance towards the left attenuates only the right channel."""
    dsp_filter = BalanceFilter(enabled=True, balance=-40)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        "pan=stereo|FL=FL|FR=0.6*FR"
    ]


def test_balance_filter_full_deflection_mutes_opposite_channel() -> None:
    """Test that full balance deflection fully mutes the opposite channel."""
    dsp_filter = BalanceFilter(enabled=True, balance=100)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        "pan=stereo|FL=0.0*FL|FR=FR"
    ]


def test_balance_filter_zero_is_passthrough() -> None:
    """Test that a centered balance filter emits no ffmpeg filter."""
    dsp_filter = BalanceFilter(enabled=True, balance=0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == []


def test_balance_filter_mono_is_skipped() -> None:
    """Test that balance is skipped on a mono source (stereo-only operation)."""
    dsp_filter = BalanceFilter(enabled=True, balance=40)
    mono_format = AudioFormat(sample_rate=48000, channels=1)
    assert filter_to_ffmpeg_params(dsp_filter, mono_format, ir_dir=IR_DIR) == []


def test_transpose_filter_octaves() -> None:
    """Test that a full octave transpose halves or doubles the pitch ratio."""
    assert filter_to_ffmpeg_params(
        TransposeFilter(enabled=True, semitones=12.0), INPUT_FORMAT, ir_dir=IR_DIR
    ) == ["rubberband=pitch=2.0:formant=preserved:pitchq=quality:window=long"]
    assert filter_to_ffmpeg_params(
        TransposeFilter(enabled=True, semitones=-12.0), INPUT_FORMAT, ir_dir=IR_DIR
    ) == ["rubberband=pitch=0.5:formant=preserved:pitchq=quality:window=long"]


def test_transpose_filter_fractional_semitones() -> None:
    """Test that a fractional transpose is supported (e.g. A=432Hz concert pitch)."""
    dsp_filter = TransposeFilter(enabled=True, semitones=-0.318)
    params = filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR)
    assert len(params) == 1
    assert isinstance(params[0], str)
    pitch, _, options = params[0].removeprefix("rubberband=pitch=").partition(":")
    assert math.isclose(float(pitch), 0.98183, rel_tol=1e-4)
    assert options == "formant=preserved:pitchq=quality:window=long"


def test_transpose_filter_zero_is_passthrough() -> None:
    """Test that a transpose filter of 0 semitones emits no ffmpeg filter."""
    dsp_filter = TransposeFilter(enabled=True, semitones=0.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == []


def test_stereo_width_filter_wide() -> None:
    """Test that a widened stereo width maps to an extrastereo filter."""
    dsp_filter = StereoWidthFilter(enabled=True, width=1.5)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        "extrastereo=m=1.5:c=0"
    ]


def test_stereo_width_filter_does_not_clip_internally() -> None:
    """Test that widening never uses the internal clipping extrastereo enables by default."""
    for width in (0.0, 0.5, 1.5, 2.0):
        dsp_filter = StereoWidthFilter(enabled=True, width=width)
        params = filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR)
        assert params == [f"extrastereo=m={width}:c=0"]


def test_stereo_width_filter_mono() -> None:
    """Test that zero width collapses the side signal to mono."""
    dsp_filter = StereoWidthFilter(enabled=True, width=0.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        "extrastereo=m=0.0:c=0"
    ]


def test_stereo_width_filter_neutral_is_passthrough() -> None:
    """Test that a unity stereo width emits no ffmpeg filter."""
    dsp_filter = StereoWidthFilter(enabled=True, width=1.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == []


def test_stereo_width_filter_mono_is_skipped() -> None:
    """Test that stereo width is skipped on a mono source (stereo-only operation)."""
    dsp_filter = StereoWidthFilter(enabled=True, width=1.5)
    mono_format = AudioFormat(sample_rate=48000, channels=1)
    assert filter_to_ffmpeg_params(dsp_filter, mono_format, ir_dir=IR_DIR) == []


def test_crossfeed_filter() -> None:
    """Test that a crossfeed filter maps to an ffmpeg crossfeed filter."""
    dsp_filter = CrossfeedFilter(enabled=True, strength=0.35, soundstage=0.6)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        "crossfeed=strength=0.35:range=0.6:level_in=1"
    ]


def test_crossfeed_filter_mono_is_skipped() -> None:
    """Test that crossfeed is skipped on a mono source (stereo-only operation)."""
    dsp_filter = CrossfeedFilter(enabled=True, strength=0.2, soundstage=0.5)
    mono_format = AudioFormat(sample_rate=48000, channels=1)
    assert filter_to_ffmpeg_params(dsp_filter, mono_format, ir_dir=IR_DIR) == []


def test_tone_control_filter() -> None:
    """Test that tone control levels map to equalizer filters, omitting zero levels."""
    dsp_filter = ToneControlFilter(enabled=True, bass_level=4.0, mid_level=0.0, treble_level=-2.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        "equalizer=frequency=100:width=200:width_type=h:gain=4.0",
        "equalizer=frequency=9000:width=18000:width_type=h:gain=-2.0",
    ]


def test_tone_control_filter_neutral_is_passthrough() -> None:
    """Test that a tone control filter with all levels at 0 emits no ffmpeg filter."""
    dsp_filter = ToneControlFilter(enabled=True, bass_level=0.0, mid_level=0.0, treble_level=0.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == []


def test_parametric_eq_preamp() -> None:
    """Test that a parametric EQ preamp maps to a volume filter."""
    dsp_filter = ParametricEQFilter(enabled=True, preamp=-3.0, per_channel_preamp={}, bands=[])
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == ["volume=-3.0dB"]


def test_parametric_eq_per_channel_preamp() -> None:
    """Test that per-channel preamp maps to a pan filter with linear per-channel gains."""
    dsp_filter = ParametricEQFilter(
        enabled=True,
        preamp=0.0,
        per_channel_preamp={AudioChannel.FL: -6.0},
        bands=[],
    )
    expected_gain = 10 ** (-6.0 / 20)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        f"pan=stereo|FL={expected_gain}*FL|FR=FR"
    ]


def test_parametric_eq_peak_band() -> None:
    """Test that an enabled peak band maps to a biquad filter with cookbook coefficients."""
    band = ParametricEQBand(
        frequency=1000.0,
        q=1.0,
        gain=3.0,
        type=ParametricEQBandType.PEAK,
        enabled=True,
        channel=AudioChannel.FL,
    )
    dsp_filter = ParametricEQFilter(enabled=True, per_channel_preamp={}, bands=[band])

    a = math.sqrt(10 ** (3.0 / 20))
    w_0 = 2 * math.pi * 1000.0 / 48000
    alpha = math.sin(w_0) / 2
    b0 = 1 + alpha * a
    b1 = -2 * math.cos(w_0)
    b2 = 1 - alpha * a
    a0 = 1 + alpha / a
    a1 = -2 * math.cos(w_0)
    a2 = 1 - alpha / a
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}:c=FL"
    ]


def test_parametric_eq_disabled_band_skipped() -> None:
    """Test that disabled parametric EQ bands are skipped."""
    band = ParametricEQBand(enabled=False, gain=6.0)
    dsp_filter = ParametricEQFilter(enabled=True, per_channel_preamp={}, bands=[band])
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == []


def _expected_pass_section(mode: HighLowPassMode, frequency: float, q: float) -> str:
    """Build the expected cookbook biquad string for one high/low-pass section."""
    w_0 = 2 * math.pi * frequency / INPUT_FORMAT.sample_rate
    alpha = math.sin(w_0) / (2 * q)
    if mode == HighLowPassMode.HIGH_PASS:
        b0 = (1 + math.cos(w_0)) / 2
        b1 = -(1 + math.cos(w_0))
        b2 = (1 + math.cos(w_0)) / 2
    else:
        b0 = (1 - math.cos(w_0)) / 2
        b1 = 1 - math.cos(w_0)
        b2 = (1 - math.cos(w_0)) / 2
    a0 = 1 + alpha
    a1 = -2 * math.cos(w_0)
    a2 = 1 - alpha
    return f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}"


def test_high_low_pass_12db_single_section() -> None:
    """Test that a 12 dB/octave high-pass is a single Butterworth biquad at Q=1/sqrt(2)."""
    dsp_filter = HighLowPassFilter(
        enabled=True, mode=HighLowPassMode.HIGH_PASS, frequency=100.0, slope=HighLowPassSlope.DB12
    )
    q = 1 / (2 * math.cos(math.pi / 4))
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        _expected_pass_section(HighLowPassMode.HIGH_PASS, 100.0, q)
    ]


def test_high_low_pass_24db_two_sections() -> None:
    """Test that a 24 dB/octave low-pass is two sections with the 4th-order Butterworth Qs."""
    dsp_filter = HighLowPassFilter(
        enabled=True, mode=HighLowPassMode.LOW_PASS, frequency=8000.0, slope=HighLowPassSlope.DB24
    )
    qs = [1 / (2 * math.cos(math.pi * (2 * k + 1) / 8)) for k in range(2)]
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        _expected_pass_section(HighLowPassMode.LOW_PASS, 8000.0, q) for q in qs
    ]


def test_high_low_pass_48db_has_four_sections() -> None:
    """Test that a 48 dB/octave filter is a cascade of four biquad sections."""
    dsp_filter = HighLowPassFilter(
        enabled=True, mode=HighLowPassMode.HIGH_PASS, frequency=40.0, slope=HighLowPassSlope.DB48
    )
    params = filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR)
    assert len(params) == 4
    assert all(isinstance(p, str) and p.startswith("biquad=") for p in params)


def test_high_low_pass_mode_changes_coefficients() -> None:
    """Test that high-pass and low-pass at identical settings produce different coefficients."""
    high = HighLowPassFilter(
        enabled=True, mode=HighLowPassMode.HIGH_PASS, frequency=1000.0, slope=HighLowPassSlope.DB12
    )
    low = HighLowPassFilter(
        enabled=True, mode=HighLowPassMode.LOW_PASS, frequency=1000.0, slope=HighLowPassSlope.DB12
    )
    assert filter_to_ffmpeg_params(high, INPUT_FORMAT, ir_dir=IR_DIR) != filter_to_ffmpeg_params(
        low, INPUT_FORMAT, ir_dir=IR_DIR
    )


def test_safety_limiter_filter() -> None:
    """Test that a safety limiter maps to an in-chain alimiter at the given ceiling."""
    dsp_filter = SafetyLimiterFilter(enabled=True, ceiling=-2.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        "alimiter=limit=-2.0dB:level=false:asc=true"
    ]


def test_compressor_filter() -> None:
    """Test that a compressor maps to acompressor with dB/ms/ratio values."""
    dsp_filter = CompressorFilter(
        enabled=True,
        threshold=-18.0,
        ratio=2.0,
        attack=20.0,
        release=250.0,
        knee=9.0,
        makeup=0.0,
    )
    expected = (
        "acompressor=threshold=-18.0dB:ratio=2.0:attack=20.0:release=250.0"
        f":knee={10 ** (9.0 / 20)}:makeup=0.0dB"
    )
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [expected]


def test_compressor_knee_db_maps_to_linear_factor() -> None:
    """Test that a knee width in dB maps to acompressor's linear knee factor."""
    # 0 dB is a hard knee, acompressor's minimum knee factor of 1.0
    hard_knee = CompressorFilter(enabled=True, knee=0.0)
    hard_params = filter_to_ffmpeg_params(hard_knee, INPUT_FORMAT, ir_dir=IR_DIR)
    assert isinstance(hard_params[0], str)
    assert ":knee=1.0:" in hard_params[0]
    # 18 dB maps to ~7.94, just under acompressor's maximum knee factor of 8
    soft_knee = CompressorFilter(enabled=True, knee=18.0)
    soft_params = filter_to_ffmpeg_params(soft_knee, INPUT_FORMAT, ir_dir=IR_DIR)
    assert isinstance(soft_params[0], str)
    assert f":knee={10 ** (18.0 / 20)}:" in soft_params[0]


def test_convolution_filter() -> None:
    """Test that a convolution filter maps to an afir fragment pulling in the IR."""
    dsp_filter = ConvolutionFilter(enabled=True, ir_id="abc123")
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        ComplexFilter(
            body="afir=irnorm=1",
            inputs=[ComplexFilterInput(path="/irs/abc123.wav", filters="aresample=48000")],
        )
    ]


def test_convolution_filter_with_gain() -> None:
    """Test that a non-zero convolution gain appends a trailing volume filter."""
    dsp_filter = ConvolutionFilter(enabled=True, ir_id="abc123", gain=3.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == [
        ComplexFilter(
            body="afir=irnorm=1",
            inputs=[ComplexFilterInput(path="/irs/abc123.wav", filters="aresample=48000")],
        ),
        "volume=3.0dB",
    ]


def test_convolution_filter_ir_path_is_not_escaped() -> None:
    """Test that the IR path is passed through verbatim, quoting characters and all."""
    dsp_filter = ConvolutionFilter(enabled=True, ir_id="abc123")
    params = filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir="/it's a dir")
    assert isinstance(params[0], ComplexFilter)
    assert params[0].inputs[0].path == "/it's a dir/abc123.wav"


def test_convolution_filter_empty_ir_id_skipped() -> None:
    """Test that a convolution filter with no impulse response selected is a no-op."""
    dsp_filter = ConvolutionFilter(enabled=True, ir_id="")
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == []


def test_convolution_filter_unsafe_ir_id_skipped() -> None:
    """Test that an ir_id outside the id format (path traversal attempt) is skipped."""
    # uppercase, accented and full-width characters all pass str.isalnum()
    for ir_id in ("../../etc/passwd", "ABC123", "abc\xe9", "\uff11\uff12\uff13"):
        dsp_filter = ConvolutionFilter(enabled=True, ir_id=ir_id)
        assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT, ir_dir=IR_DIR) == []
