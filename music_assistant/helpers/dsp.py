"""Helper functions for DSP filters."""

import math

from music_assistant_models.dsp import (
    DSPFilter,
    ParametricEQBandType,
    ParametricEQFilter,
    ToneControlFilter,
)
from music_assistant_models.streamdetails import AudioFormat

# ruff: noqa: PLR0915


def filter_to_ffmpeg_params(dsp_filter: DSPFilter, input_format: AudioFormat) -> list[str]:
    """Convert a DSP filter model to FFmpeg filter parameters.

    Args:
        dsp_filter: DSP filter configuration (ParametricEQ or ToneControl)
        input_format: Audio format containing sample rate

    Returns:
        List of FFmpeg filter parameter strings
    """
    filter_params = []

    if isinstance(dsp_filter, ParametricEQFilter):
        if dsp_filter.preamp != 0:
            filter_params.append(f"volume={dsp_filter.preamp}dB")
        for b in dsp_filter.bands:
            if not b.enabled:
                continue
            # From https://webaudio.github.io/Audio-EQ-Cookbook/audio-eq-cookbook.html

            f_s = input_format.sample_rate
            f_0 = b.frequency
            db_gain = b.gain
            q = b.q

            a = math.sqrt(10 ** (db_gain / 20))
            w_0 = 2 * math.pi * f_0 / f_s
            alpha = math.sin(w_0) / (2 * q)

            if b.type == ParametricEQBandType.PEAK:
                b0 = 1 + alpha * a
                b1 = -2 * math.cos(w_0)
                b2 = 1 - alpha * a
                a0 = 1 + alpha / a
                a1 = -2 * math.cos(w_0)
                a2 = 1 - alpha / a

                filter_params.append(f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}")
            elif b.type == ParametricEQBandType.LOW_SHELF:
                b0 = a * ((a + 1) - (a - 1) * math.cos(w_0) + 2 * math.sqrt(a) * alpha)
                b1 = 2 * a * ((a - 1) - (a + 1) * math.cos(w_0))
                b2 = a * ((a + 1) - (a - 1) * math.cos(w_0) - 2 * math.sqrt(a) * alpha)
                a0 = (a + 1) + (a - 1) * math.cos(w_0) + 2 * math.sqrt(a) * alpha
                a1 = -2 * ((a - 1) + (a + 1) * math.cos(w_0))
                a2 = (a + 1) + (a - 1) * math.cos(w_0) - 2 * math.sqrt(a) * alpha

                filter_params.append(f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}")
            elif b.type == ParametricEQBandType.HIGH_SHELF:
                b0 = a * ((a + 1) + (a - 1) * math.cos(w_0) + 2 * math.sqrt(a) * alpha)
                b1 = -2 * a * ((a - 1) + (a + 1) * math.cos(w_0))
                b2 = a * ((a + 1) + (a - 1) * math.cos(w_0) - 2 * math.sqrt(a) * alpha)
                a0 = (a + 1) - (a - 1) * math.cos(w_0) + 2 * math.sqrt(a) * alpha
                a1 = 2 * ((a - 1) - (a + 1) * math.cos(w_0))
                a2 = (a + 1) - (a - 1) * math.cos(w_0) - 2 * math.sqrt(a) * alpha

                filter_params.append(f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}")
            elif b.type == ParametricEQBandType.HIGH_PASS:
                b0 = (1 + math.cos(w_0)) / 2
                b1 = -(1 + math.cos(w_0))
                b2 = (1 + math.cos(w_0)) / 2
                a0 = 1 + alpha
                a1 = -2 * math.cos(w_0)
                a2 = 1 - alpha

                filter_params.append(f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}")
            elif b.type == ParametricEQBandType.LOW_PASS:
                b0 = (1 - math.cos(w_0)) / 2
                b1 = 1 - math.cos(w_0)
                b2 = (1 - math.cos(w_0)) / 2
                a0 = 1 + alpha
                a1 = -2 * math.cos(w_0)
                a2 = 1 - alpha

                filter_params.append(f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}")
            elif b.type == ParametricEQBandType.NOTCH:
                b0 = 1
                b1 = -2 * math.cos(w_0)
                b2 = 1
                a0 = 1 + alpha
                a1 = -2 * math.cos(w_0)
                a2 = 1 - alpha

                filter_params.append(f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}")
    if isinstance(dsp_filter, ToneControlFilter):
        # A basic 3-band equalizer
        if dsp_filter.bass_level != 0:
            filter_params.append(
                f"equalizer=frequency=100:width=200:width_type=h:gain={dsp_filter.bass_level}"
            )
        if dsp_filter.mid_level != 0:
            filter_params.append(
                f"equalizer=frequency=900:width=1800:width_type=h:gain={dsp_filter.mid_level}"
            )
        if dsp_filter.treble_level != 0:
            filter_params.append(
                f"equalizer=frequency=9000:width=18000:width_type=h:gain={dsp_filter.treble_level}"
            )

    return filter_params
