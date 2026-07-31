"""Helper functions for DSP filters."""

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from music_assistant_models.dsp import (
    AudioChannel,
    BalanceFilter,
    CrossfeedFilter,
    DSPFilter,
    GainFilter,
    HighLowPassFilter,
    HighLowPassMode,
    ParametricEQBandType,
    ParametricEQFilter,
    StereoWidthFilter,
    ToneControlFilter,
    TransposeFilter,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items.audio_format import AudioFormat

# ruff: noqa: PLR0915


@dataclass(slots=True)
class ComplexFilter:
    """
    A DSP filter fragment that pulls in one or more extra source inputs.

    Represents a chain entry that cannot be expressed as a plain single-input
    filter string, such as an FFmpeg ``afir`` convolution that needs an
    impulse-response input.

    :param body: The filter consuming the main input followed by each source in
        order (e.g. "afir=gtype=gn").
    :param sources: Sub-chains that each produce one extra input for ``body``
        (e.g. "amovie='/x/ir.wav',aresample=48000").
    """

    body: str
    sources: list[str] = field(default_factory=list)


def filter_to_ffmpeg_params(dsp_filter: DSPFilter, input_format: AudioFormat) -> list[str]:
    """
    Convert a DSP filter model to FFmpeg filter parameters.

    Args:
        dsp_filter: DSP filter configuration
        input_format: Audio format containing sample rate

    Returns:
        List of FFmpeg filter parameter strings
    """
    filter_params = []

    if isinstance(dsp_filter, ParametricEQFilter):
        has_per_channel_preamp = any(value != 0 for value in dsp_filter.per_channel_preamp.values())
        if dsp_filter.preamp and dsp_filter.preamp != 0 and not has_per_channel_preamp:
            filter_params.append(f"volume={dsp_filter.preamp}dB")
        # "volume" is handled for the whole audio stream only, so we'll use the pan filter instead
        elif has_per_channel_preamp:
            channel_config = []
            all_channels = [AudioChannel.FL, AudioChannel.FR]
            for channel_id in all_channels:
                # Get gain for this channel, default to 0 if not specified
                gain_db = dsp_filter.per_channel_preamp.get(channel_id, 0)
                # Apply both the overall preamp and the per-channel preamp
                total_gain_db = (
                    dsp_filter.preamp + gain_db if dsp_filter.preamp is not None else gain_db
                )
                if total_gain_db != 0:
                    # Convert dB to linear gain
                    gain = 10 ** (total_gain_db / 20)
                    channel_config.append(f"{channel_id}={gain}*{channel_id}")
                else:
                    channel_config.append(f"{channel_id}={channel_id}")

            # Could potentially also be expanded for more than 2 channels
            filter_params.append("pan=stereo|" + "|".join(channel_config))
        for b in dsp_filter.bands:
            if not b.enabled:
                continue
            channels = ""
            if b.channel != AudioChannel.ALL:
                channels = f":c={b.channel}"
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

                filter_params.append(
                    f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}{channels}"
                )
            elif b.type == ParametricEQBandType.LOW_SHELF:
                b0 = a * ((a + 1) - (a - 1) * math.cos(w_0) + 2 * math.sqrt(a) * alpha)
                b1 = 2 * a * ((a - 1) - (a + 1) * math.cos(w_0))
                b2 = a * ((a + 1) - (a - 1) * math.cos(w_0) - 2 * math.sqrt(a) * alpha)
                a0 = (a + 1) + (a - 1) * math.cos(w_0) + 2 * math.sqrt(a) * alpha
                a1 = -2 * ((a - 1) + (a + 1) * math.cos(w_0))
                a2 = (a + 1) + (a - 1) * math.cos(w_0) - 2 * math.sqrt(a) * alpha

                filter_params.append(
                    f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}{channels}"
                )
            elif b.type == ParametricEQBandType.HIGH_SHELF:
                b0 = a * ((a + 1) + (a - 1) * math.cos(w_0) + 2 * math.sqrt(a) * alpha)
                b1 = -2 * a * ((a - 1) + (a + 1) * math.cos(w_0))
                b2 = a * ((a + 1) + (a - 1) * math.cos(w_0) - 2 * math.sqrt(a) * alpha)
                a0 = (a + 1) - (a - 1) * math.cos(w_0) + 2 * math.sqrt(a) * alpha
                a1 = 2 * ((a - 1) - (a + 1) * math.cos(w_0))
                a2 = (a + 1) - (a - 1) * math.cos(w_0) - 2 * math.sqrt(a) * alpha

                filter_params.append(
                    f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}{channels}"
                )
            elif b.type == ParametricEQBandType.HIGH_PASS:
                filter_params.append(
                    _pass_biquad_params(high_pass=True, w_0=w_0, alpha=alpha, channels=channels)
                )
            elif b.type == ParametricEQBandType.LOW_PASS:
                filter_params.append(
                    _pass_biquad_params(high_pass=False, w_0=w_0, alpha=alpha, channels=channels)
                )
            elif b.type == ParametricEQBandType.NOTCH:
                b0 = 1
                b1 = -2 * math.cos(w_0)
                b2 = 1
                a0 = 1 + alpha
                a1 = -2 * math.cos(w_0)
                a2 = 1 - alpha

                filter_params.append(
                    f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}{channels}"
                )
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
    if isinstance(dsp_filter, GainFilter) and dsp_filter.gain != 0:
        filter_params.append(f"volume={dsp_filter.gain}dB")
    if isinstance(dsp_filter, BalanceFilter) and dsp_filter.balance != 0:
        # balance is a stereo operation; on a non-stereo source the FL/FR pan
        # expression would output silence, so only apply it to stereo streams
        if input_format.channels == 2:
            # attenuate only the channel opposite the slider direction, so there is
            # no positive gain and thus no clipping risk
            attenuation = (100 - abs(dsp_filter.balance)) / 100
            if dsp_filter.balance > 0:
                filter_params.append(f"pan=stereo|FL={attenuation}*FL|FR=FR")
            else:
                filter_params.append(f"pan=stereo|FL=FL|FR={attenuation}*FR")
    if isinstance(dsp_filter, TransposeFilter) and dsp_filter.semitones != 0:
        # rubberband expects a frequency ratio rather than a number of semitones
        pitch = 2 ** (dsp_filter.semitones / 12)
        # preserving formants keeps voices natural instead of chipmunk-like; revisit
        # these quality options if they prove too costly on low powered hardware
        filter_params.append(
            f"rubberband=pitch={pitch}:formant=preserved:pitchq=quality:window=long"
        )

    if isinstance(dsp_filter, HighLowPassFilter):
        # A high/low-pass of a given slope is a cascade of second-order Butterworth
        # sections, each adding 12 dB/octave, at the same cutoff but different Q.
        # slope is validated to 12, 24 or 48 dB/octave, so order is 2, 4 or 8.
        high_pass = dsp_filter.mode == HighLowPassMode.HIGH_PASS
        order = dsp_filter.slope // 6
        w_0 = 2 * math.pi * dsp_filter.frequency / input_format.sample_rate
        sin_w0 = math.sin(w_0)
        for section in range(order // 2):
            # Butterworth pole Q for this section
            q = 1 / (2 * math.cos(math.pi * (2 * section + 1) / (2 * order)))
            alpha = sin_w0 / (2 * q)
            filter_params.append(_pass_biquad_params(high_pass=high_pass, w_0=w_0, alpha=alpha))

    if isinstance(dsp_filter, StereoWidthFilter) and dsp_filter.width != 1.0:
        # width scales the side (L-R) signal; a non-stereo source has no side
        # component, so only apply it to stereo streams
        if input_format.channels == 2:
            # c=0 disables the internal hard clipping extrastereo applies by default,
            # which would clamp a widened signal before any later filter or the output
            # gain can bring it down. The float internal format exists for that headroom
            filter_params.append(f"extrastereo=m={dsp_filter.width}:c=0")

    if isinstance(dsp_filter, CrossfeedFilter):
        # crossfeed blends the left and right channels for headphone listening
        # and is only meaningful on stereo streams
        if input_format.channels == 2:
            # crossfeed is turned off by disabling the filter, never by a zero strength
            # level_in defaults to 0.9, which would attenuate every crossfed stream
            filter_params.append(
                f"crossfeed=strength={dsp_filter.strength}:range={dsp_filter.soundstage}:level_in=1"
            )

    return filter_params


def _pass_biquad_params(*, high_pass: bool, w_0: float, alpha: float, channels: str = "") -> str:
    """
    Build the FFmpeg biquad parameters for one high-pass or low-pass section.

    :param high_pass: True for a high-pass section, False for a low-pass section.
    :param w_0: Normalised angular cutoff frequency, 2*pi*frequency/sample_rate.
    :param alpha: Cookbook alpha term, sin(w_0)/(2*Q).
    :param channels: Optional FFmpeg channel selector suffix, e.g. ":c=FL".
    """
    cos_w0 = math.cos(w_0)
    if high_pass:
        b0 = (1 + cos_w0) / 2
        b1 = -(1 + cos_w0)
        b2 = (1 + cos_w0) / 2
    else:
        b0 = (1 - cos_w0) / 2
        b1 = 1 - cos_w0
        b2 = (1 - cos_w0) / 2
    a0 = 1 + alpha
    a1 = -2 * cos_w0
    a2 = 1 - alpha
    return f"biquad=b0={b0}:b1={b1}:b2={b2}:a0={a0}:a1={a1}:a2={a2}{channels}"
