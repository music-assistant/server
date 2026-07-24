"""Tests for the DSP helpers."""

import math

from music_assistant_models.dsp import (
    AudioChannel,
    BalanceFilter,
    GainFilter,
    ParametricEQBand,
    ParametricEQBandType,
    ParametricEQFilter,
    ToneControlFilter,
)
from music_assistant_models.media_items.audio_format import AudioFormat

from music_assistant.helpers.dsp import filter_to_ffmpeg_params

INPUT_FORMAT = AudioFormat(sample_rate=48000)


def test_gain_filter() -> None:
    """Test that a gain filter maps to a volume filter in dB."""
    dsp_filter = GainFilter(enabled=True, gain=5.5)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == ["volume=5.5dB"]
    dsp_filter = GainFilter(enabled=True, gain=-15.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == ["volume=-15.0dB"]


def test_gain_filter_zero_is_passthrough() -> None:
    """Test that a gain filter with 0 dB gain emits no ffmpeg filter."""
    dsp_filter = GainFilter(enabled=True, gain=0.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == []


def test_balance_filter_right() -> None:
    """Test that balance towards the right attenuates only the left channel."""
    dsp_filter = BalanceFilter(enabled=True, balance=40)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == ["pan=stereo|FL=0.6*FL|FR=FR"]


def test_balance_filter_left() -> None:
    """Test that balance towards the left attenuates only the right channel."""
    dsp_filter = BalanceFilter(enabled=True, balance=-40)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == ["pan=stereo|FL=FL|FR=0.6*FR"]


def test_balance_filter_full_deflection_mutes_opposite_channel() -> None:
    """Test that full balance deflection fully mutes the opposite channel."""
    dsp_filter = BalanceFilter(enabled=True, balance=100)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == ["pan=stereo|FL=0.0*FL|FR=FR"]


def test_balance_filter_zero_is_passthrough() -> None:
    """Test that a centered balance filter emits no ffmpeg filter."""
    dsp_filter = BalanceFilter(enabled=True, balance=0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == []


def test_balance_filter_mono_is_skipped() -> None:
    """Test that balance is skipped on a mono source (stereo-only operation)."""
    dsp_filter = BalanceFilter(enabled=True, balance=40)
    mono_format = AudioFormat(sample_rate=48000, channels=1)
    assert filter_to_ffmpeg_params(dsp_filter, mono_format) == []


def test_tone_control_filter() -> None:
    """Test that tone control levels map to equalizer filters, omitting zero levels."""
    dsp_filter = ToneControlFilter(enabled=True, bass_level=4.0, mid_level=0.0, treble_level=-2.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == [
        "equalizer=frequency=100:width=200:width_type=h:gain=4.0",
        "equalizer=frequency=9000:width=18000:width_type=h:gain=-2.0",
    ]


def test_tone_control_filter_neutral_is_passthrough() -> None:
    """Test that a tone control filter with all levels at 0 emits no ffmpeg filter."""
    dsp_filter = ToneControlFilter(enabled=True, bass_level=0.0, mid_level=0.0, treble_level=0.0)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == []


def test_parametric_eq_preamp() -> None:
    """Test that a parametric EQ preamp maps to a volume filter."""
    dsp_filter = ParametricEQFilter(enabled=True, preamp=-3.0, per_channel_preamp={}, bands=[])
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == ["volume=-3.0dB"]


def test_parametric_eq_per_channel_preamp() -> None:
    """Test that per-channel preamp maps to a pan filter with linear per-channel gains."""
    dsp_filter = ParametricEQFilter(
        enabled=True,
        preamp=0.0,
        per_channel_preamp={AudioChannel.FL: -6.0},
        bands=[],
    )
    expected_gain = 10 ** (-6.0 / 20)
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == [
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
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == [
        f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}:c=FL"
    ]


def test_parametric_eq_disabled_band_skipped() -> None:
    """Test that disabled parametric EQ bands are skipped."""
    band = ParametricEQBand(enabled=False, gain=6.0)
    dsp_filter = ParametricEQFilter(enabled=True, per_channel_preamp={}, bands=[band])
    assert filter_to_ffmpeg_params(dsp_filter, INPUT_FORMAT) == []
