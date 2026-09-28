"""
Build WLED's native "Audio Sync" UDP packet.

Wire format taken from WLED's `usermods/audioreactive/audio_reactive.cpp`
(`struct __attribute__((packed)) audioSyncPacket`), header version "00002",
44 bytes total, sent to multicast group 239.0.0.1 on a per-zone port.
"""

from __future__ import annotations

import math
import struct

from .constants import DEFAULT_SCALING_MODE, ScalingMode

# "<" = little-endian, no padding (mirrors the C struct's __attribute__((packed))).
_STRUCT_FORMAT: str = "<6s2sffBB16sHff"
_HEADER: bytes = b"00002\x00"
_FFT_BINS: int = 16

# Approximate scale of WLED's unnormalized FFT_Magnitude field, from observed device values.
FFT_MAGNITUDE_SCALE: float = 16000.0

PACKET_SIZE: int = struct.calcsize(_STRUCT_FORMAT)  # == 44, matches WLED's packed C struct

# Per-band high-frequency boost divisors, taken from WLED's FFTScalingMode.
_BAND_MULTIPLIER_DIVISOR: dict[ScalingMode, float] = {
    ScalingMode.LINEAR: 1.8,
    ScalingMode.SQUARE_ROOT: 4.5,
    ScalingMode.LOGARITHMIC: 18.0,
}

# Steepness of the logarithmic curve (see _apply_curve), matching WLED's shape on a [0, 1] input.
_LOG_STEEPNESS: float = 99.0

# Amplitudes below this are zeroed before the curve, so silence stays at exactly 0 (as in WLED).
NOISE_GATE: float = 0.02

# Cancels the A-weighting the Sendspin extractor applies per band, since WLED's GEQ is unweighted.
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

# WLED's pink noise compensation (fftResultPink) in dB, applied before the curve.
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
    """Convert a Sendspin dB-linear uint16 value to gain-boosted linear amplitude."""
    if value <= 0:
        return 0.0
    normalized_db = max(0.0, min(1.0, value / 65535.0))
    db = normalized_db * 60.0 - 60.0
    amplitude = float(10.0 ** (db / 20.0))
    boosted = amplitude * float(10.0 ** (gain_db / 20.0))
    return max(0.0, min(1.0, boosted))


def _apply_curve(amplitude: float, scaling_mode: ScalingMode) -> float:
    """Apply a WLED-style perceptual curve to a linear amplitude in [0, 1]."""
    if scaling_mode == ScalingMode.LINEAR:
        return amplitude
    if scaling_mode == ScalingMode.LOGARITHMIC:
        return float(math.log1p(_LOG_STEEPNESS * amplitude) / math.log1p(_LOG_STEEPNESS))
    return float(amplitude**0.5)  # square_root


def _band_multiplier(band_index: int, scaling_mode: ScalingMode) -> float:
    """Return the per-band high-frequency boost for a scaling mode."""
    divisor = _BAND_MULTIPLIER_DIVISOR[scaling_mode]
    return 0.85 + band_index / divisor


def fft_magnitude_from_amplitude(f_peak_amp: int, gain_db: float = 0.0) -> float:
    """
    Convert a Sendspin f_peak_amp value to WLED's FFT_Magnitude scale.

    :param f_peak_amp: Dominant-bin amplitude in [0, 65535] as reported by the visualizer.
    :param gain_db: Gain to apply before scaling, in dB.
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

    :param loudness: Loudness value in [0, 65535] as reported by the visualizer.
    :param gain_db: Gain to apply before scaling, in dB.
    :param scaling_mode: Which perceptual curve to apply.
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

    :param bins: Spectrum magnitudes, one per requested band.
    :param gain_db: Gain to apply before scaling, in dB.
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
    :param sample_peak: Onset flag, True only on the tick a fresh onset was detected.
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
