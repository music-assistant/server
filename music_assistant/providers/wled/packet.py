"""
Build WLED's native "Audio Sync" UDP packet.

Wire format taken from WLED's `usermods/audioreactive/audio_reactive.cpp`
(`struct __attribute__((packed)) audioSyncPacket`), header version "00002",
44 bytes total, sent to multicast group 239.0.0.1 on a per-zone port.
"""

from __future__ import annotations

import math
import struct
from typing import Literal

# "<" = little-endian, no padding (mirrors the C struct's __attribute__((packed))).
_STRUCT_FORMAT: str = "<6s2sffBB16sHff"
_HEADER: bytes = b"00002\x00"
_FFT_BINS: int = 16

PACKET_SIZE: int = struct.calcsize(_STRUCT_FORMAT)  # == 44, matches WLED's packed C struct

ScalingMode = Literal["logarithmic", "linear", "square_root"]

# WLED's own onboard analysis offers exactly these three curves for turning
# raw FFT magnitude into a byte (audio_reactive.cpp FFTScalingMode); there is
# no single "correct" one -- which looks best depends on music style and
# personal taste, so this provider exposes the same choice rather than
# picking one. WLED only applies these curves to FFT it computes locally; a
# UDP Sync *receiver* copies received fftResult bytes verbatim with no
# correction of its own, so this provider is fully responsible for applying
# a curve before sending.
DEFAULT_SCALING_MODE: ScalingMode = "square_root"

# Each mode also carries its own per-band multiplier in WLED's source
# (`currentResult *= 0.85f + (float(i)/divisor)`), boosting high-frequency
# bands that would otherwise look weak relative to bass: linear boosts the
# hardest (up to ~9.18x at band 15), square root moderately (~4.18x),
# logarithmic gently (~1.68x) since the log curve itself already lifts quiet
# content a lot. Real WLED constants are calibrated against its own raw
# 0-1023 internal magnitude scale; the divisors themselves (not the whole
# formula) are the part that transfers directly to our normalized [0, 1]
# amplitude, since they only shape the *relative* per-band ratio.
_BAND_MULTIPLIER_DIVISOR: dict[ScalingMode, float] = {
    "linear": 1.8,
    "square_root": 4.5,
    "logarithmic": 18.0,
}

# Steepness of the logarithmic curve's log1p mapping (see _apply_curve). Not
# a literal transcription of WLED's own log() constants, which are
# calibrated for its unnormalized internal scale -- this reproduces the same
# *shape* (log compresses quiet content harder than sqrt does) on our [0, 1]
# input.
_LOG_STEEPNESS: float = 99.0

# A compressive curve amplifies small values disproportionately (sqrt(0.001)
# ~= 0.032, a 32x relative jump), which would reintroduce a milder version of
# the "silence isn't zero" problem the curve is otherwise fixing. WLED's own
# logarithmic/square-root FFTScalingModes avoid this the same way: they
# hard-zero anything below a fixed threshold *before* applying the curve,
# rather than compressing the noise floor into visibility.
NOISE_GATE: float = 0.02

# WLED's own onboard GEQ display uses raw FFT magnitude with no frequency
# weighting (audio_reactive.cpp's FFTScalingMode only ever operates on raw
# fftCalc[]/fftAvg[]). The Sendspin visualizer extractor instead applies
# A-weighting -- a standard "how loud does this sound to human ears" curve --
# to the same magnitude before binning it into the spectrum, appropriate for
# its own loudness feature but not for a raw GEQ display: it attenuates our
# lowest band by ~30dB, which would otherwise read as persistently near-zero
# even when a real WLED device (unweighted) shows strong bass energy there.
# This table cancels that out per-band, restoring the raw-magnitude behavior
# WLED's own analysis has. Values are the negated A-weighting gain (IEC
# 61672, aiosendspin's own formula) at each band's approximate center
# frequency (geometric mean of its edges in our own log-scale band table,
# SPECTRUM_F_MIN=43/SPECTRUM_F_MAX=9259/SPECTRUM_BINS=16 in constants.py).
_A_WEIGHT_COMPENSATION_DB: tuple[float, ...] = (
    30.0,
    24.2,
    19.2,
    14.9,
    11.1,
    7.9,
    5.1,
    2.8,
    1.1,
    -0.1,
    -0.9,
    -1.2,
    -1.2,
    -1.0,
    -0.3,
    1.0,
)

# WLED's own "pink noise" compensation, applied to raw magnitude before any
# curve (audio_reactive.cpp: `fftCalc[i] *= fftResultPink[i]`) -- a second,
# separate per-band correction from the per-mode `0.85 + i/divisor` boost
# _band_multiplier applies (that one runs *after* the curve; this one runs
# *before*, same pipeline position as WLED's own). Converted from WLED's
# linear multipliers (1.70, 1.71, ..., 9.55) to dB (20*log10(x)) so it can
# be summed with _A_WEIGHT_COMPENSATION_DB and gain_db as one combined
# per-band gain.
_PINK_NOISE_COMPENSATION_DB: tuple[float, ...] = (
    4.61,
    4.66,
    4.76,
    5.01,
    4.51,
    3.86,
    3.81,
    4.24,
    5.06,
    4.19,
    5.11,
    6.28,
    7.85,
    10.50,
    16.69,
    19.60,
)


def _amplitude_from_dbu16(value: int, gain_db: float) -> float:
    """
    Convert a Sendspin dB-linear uint16 value to gain-boosted linear amplitude.

    The extractor's dB mapping is linear-in-dB against a fixed, absolute
    full-scale-sine reference (0 == -60 dBFS, 65535 == 0 dBFS). Real program
    material -- especially a loudness-normalized internet radio stream --
    rarely gets anywhere near that theoretical ceiling even when it sounds
    loud, so without a gain boost the whole signal sits compressed low.

    ``gain_db`` is applied as a proper amplitude-domain multiplier
    (``10**(gain_db/20)``), not a flat shift on the dB-linear value --
    multiplying amplitude preserves true silence as exactly zero, whereas
    adding a flat offset to the dB-linear representation would lift even a
    silent (floor) value away from zero.

    :param value: Loudness/spectrum-bin/f_peak_amp value in [0, 65535].
    :param gain_db: Gain to apply, in dB.
    """
    normalized_db = max(0.0, min(1.0, value / 65535.0))
    db = normalized_db * 60.0 - 60.0
    amplitude = float(10.0 ** (db / 20.0))
    boosted = amplitude * float(10.0 ** (gain_db / 20.0))
    return max(0.0, min(1.0, boosted))


def _apply_curve(amplitude: float, scaling_mode: ScalingMode) -> float:
    """
    Apply a WLED-style perceptual curve to a linear amplitude in [0, 1].

    :param amplitude: Linear amplitude, already gain-boosted and clipped.
    :param scaling_mode: Which of WLED's three FFTScalingMode curves to use.
    """
    if scaling_mode == "linear":
        return amplitude
    if scaling_mode == "logarithmic":
        return float(math.log1p(_LOG_STEEPNESS * amplitude) / math.log1p(_LOG_STEEPNESS))
    return float(amplitude**0.5)  # square_root


def _band_multiplier(band_index: int, scaling_mode: ScalingMode) -> float:
    """Return the per-band high-frequency boost for a given mode (see _BAND_MULTIPLIER_DIVISOR)."""
    divisor = _BAND_MULTIPLIER_DIVISOR[scaling_mode]
    return 0.85 + band_index / divisor


# WLED's FFT_Magnitude field is not byte-clamped like fftResult/sampleRaw --
# it's a raw, unnormalized float on WLED's own ADC/FFT-window scale, routinely
# in the thousands for real hardware. There's no documented "correct" target
# (it's whatever WLED's own effects happen to compare it against), so this
# scales to a comparable order of magnitude rather than compressing it into
# a 0-255-ish range like loudness_to_sample would -- passing a tiny value
# there would make magnitude-based effects under-react. Approximate, based
# on observed real-device values; may need further on-device tuning.
FFT_MAGNITUDE_SCALE: float = 16000.0


def fft_magnitude_from_amplitude(f_peak_amp: int, gain_db: float = 0.0) -> float:
    """
    Convert a Sendspin f_peak_amp value to WLED's FFT_Magnitude scale.

    Unlike ``loudness_to_sample``, this does not clamp to a 0-255-ish byte
    range or apply a scaling-mode curve -- WLED's own FFT_Magnitude is raw,
    uncurved magnitude (see ``FFT_MAGNITUDE_SCALE``).

    :param f_peak_amp: Dominant-bin amplitude in [0, 65535] as reported by the visualizer.
    :param gain_db: Gain to apply before scaling (see _amplitude_from_dbu16).
    """
    amplitude = _amplitude_from_dbu16(f_peak_amp, gain_db)
    if amplitude < NOISE_GATE:
        return 0.0
    return amplitude * FFT_MAGNITUDE_SCALE


def loudness_to_sample(
    loudness: int, gain_db: float = 0.0, scaling_mode: ScalingMode = DEFAULT_SCALING_MODE
) -> float:
    """
    Convert a Sendspin ``ExtractedFrame.loudness`` value to WLED's sample scale.

    WLED's sampleRaw/sampleSmth fields are linear amplitude on a roughly
    0-255 scale. Values below ``NOISE_GATE`` are hard-zeroed before the
    curve is applied, so true silence stays at exactly 0 regardless of gain
    or curve choice.

    :param loudness: Loudness value in [0, 65535] as reported by the visualizer.
    :param gain_db: Gain to apply before scaling (see _amplitude_from_dbu16).
    :param scaling_mode: Which perceptual curve to apply (see _apply_curve).
    """
    amplitude = _amplitude_from_dbu16(loudness, gain_db)
    if amplitude < NOISE_GATE:
        return 0.0
    return _apply_curve(amplitude, scaling_mode) * 255.0


def spectrum_to_fft_result(
    bins: list[int], gain_db: float = 0.0, scaling_mode: ScalingMode = DEFAULT_SCALING_MODE
) -> bytes:
    """
    Convert Sendspin spectrum bins (0-65535 each) to WLED's fftResult[16] (0-255 each).

    Each bin goes through the same dB-to-amplitude conversion as
    ``loudness_to_sample``, plus two WLED-matching per-band corrections
    before the curve (A-weighting cancellation, _A_WEIGHT_COMPENSATION_DB;
    and pink-noise compensation, _PINK_NOISE_COMPENSATION_DB) and one after
    it (the per-mode high-frequency boost, see _BAND_MULTIPLIER_DIVISOR).

    :param bins: Spectrum magnitudes, one per requested band.
    :param gain_db: Gain to apply before scaling (see _amplitude_from_dbu16).
    :param scaling_mode: Which perceptual curve and per-band boost to apply.
    """
    result = bytearray(_FFT_BINS)
    for i, bin_value in enumerate(bins[:_FFT_BINS]):
        band_gain_db = gain_db + _A_WEIGHT_COMPENSATION_DB[i] + _PINK_NOISE_COMPENSATION_DB[i]
        amplitude = _amplitude_from_dbu16(bin_value, band_gain_db)
        if amplitude >= NOISE_GATE:
            curved = _apply_curve(amplitude, scaling_mode) * _band_multiplier(i, scaling_mode)
            result[i] = max(0, min(255, round(curved * 255.0)))
    return bytes(result)


def pack_audio_sync_packet(
    *,
    sample_raw: float,
    sample_smth: float,
    sample_peak: bool,
    fft_result: bytes,
    fft_magnitude: float,
    fft_major_peak: float,
) -> bytes:
    """
    Pack a 44-byte WLED audioSyncPacket.

    :param sample_raw: Instantaneous volume sample, WLED's ~0-255 scale.
    :param sample_smth: Smoothed volume sample, WLED's ~0-255 scale.
    :param sample_peak: Momentary onset flag -- True only on the tick a fresh
        onset was detected; WLED latches and resets this itself, so it must
        not be held True across ticks.
    :param fft_result: Exactly 16 bytes, one amplitude (0-255) per FFT band.
    :param fft_magnitude: Magnitude of the dominant frequency bin.
    :param fft_major_peak: Frequency in Hz of the dominant frequency bin.
    """
    if len(fft_result) != _FFT_BINS:
        raise ValueError(f"fft_result must be exactly {_FFT_BINS} bytes, got {len(fft_result)}")
    return struct.pack(
        _STRUCT_FORMAT,
        _HEADER,
        b"\x00\x00",
        sample_raw,
        sample_smth,
        1 if sample_peak else 0,
        0,
        fft_result,
        0,
        fft_magnitude,
        fft_major_peak,
    )
