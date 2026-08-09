"""
Build WLED's native "Audio Sync" UDP packet.

Wire format taken from WLED's `usermods/audioreactive/audio_reactive.cpp`
(`struct __attribute__((packed)) audioSyncPacket`), header version "00002",
44 bytes total, sent to multicast group 239.0.0.1 on a per-zone port.
"""

from __future__ import annotations

import struct

# "<" = little-endian, no padding (mirrors the C struct's __attribute__((packed))).
_STRUCT_FORMAT: str = "<6s2sffBB16sHff"
_HEADER: bytes = b"00002\x00"
_FFT_BINS: int = 16

PACKET_SIZE: int = struct.calcsize(_STRUCT_FORMAT)  # == 44, matches WLED's packed C struct


# Square-root perceptual compression, matching WLED's own "square root scaling"
# FFTScalingMode -- one of its three built-in curves for turning FFT magnitude
# into a visually lively byte value. Pure linear amplitude (gamma=1.0) makes
# everything except near-peak content look dim, since loudness perception
# itself is compressive, not linear. WLED only applies its scaling curves to
# FFT it computes locally; a UDP Sync receiver copies received fftResult
# bytes verbatim with no correction of its own (audio_reactive.cpp), so this
# provider is fully responsible for choosing a visually sane curve.
PERCEPTUAL_GAMMA: float = 0.5

# A compressive curve amplifies small values disproportionately (sqrt(0.001)
# ~= 0.032, a 32x relative jump), which would quietly reintroduce a milder
# version of the "silence isn't zero" problem the gamma curve is otherwise
# fixing. WLED's own square-root FFTScalingMode avoids this the same way:
# it hard-zeroes anything below a fixed threshold *before* applying sqrt(),
# rather than compressing the noise floor into visibility.
NOISE_GATE: float = 0.02


def _dbu16_to_amplitude(value: int, gain_db: float = 0.0, gamma: float = PERCEPTUAL_GAMMA) -> float:
    """
    Convert a Sendspin dB-linear uint16 value to a perceptually-scaled amplitude.

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

    Values below ``NOISE_GATE`` are hard-zeroed before the gamma curve is
    applied (see ``NOISE_GATE``), then ``gamma`` compresses what remains
    (``amplitude ** gamma``) to spread out quiet-to-moderate content.

    :param value: Loudness/spectrum-bin/f_peak_amp value in [0, 65535].
    :param gain_db: Gain to apply, in dB.
    :param gamma: Perceptual compression exponent; 1.0 is pure linear amplitude.
    """
    normalized_db = max(0.0, min(1.0, value / 65535.0))
    db = normalized_db * 60.0 - 60.0
    amplitude = float(10.0 ** (db / 20.0))
    boosted = amplitude * float(10.0 ** (gain_db / 20.0))
    clipped = max(0.0, min(1.0, boosted))
    if clipped < NOISE_GATE:
        return 0.0
    return float(clipped**gamma)


def loudness_to_sample(loudness: int, gain_db: float = 0.0) -> float:
    """
    Convert a Sendspin ``ExtractedFrame.loudness`` value to WLED's sample scale.

    WLED's sampleRaw/sampleSmth fields are linear amplitude on a roughly
    0-255 scale, so the dB value is converted back through amplitude before
    rescaling -- a direct linear rescale would peg normal-volume audio near
    the top of the range.

    :param loudness: Loudness value in [0, 65535] as reported by the visualizer.
    :param gain_db: Gain to apply before scaling (see _dbu16_to_amplitude).
    """
    return _dbu16_to_amplitude(loudness, gain_db) * 255.0


def spectrum_to_fft_result(bins: list[int], gain_db: float = 0.0) -> bytes:
    """
    Convert Sendspin spectrum bins (0-65535 each) to WLED's fftResult[16] (0-255 each).

    Each bin is a dB-linear value like ``loudness``, so it goes through the
    same dB-to-amplitude conversion before scaling to a byte -- displaying
    the raw dB-linear value directly would make quiet bands look far more
    prominent than they should (dB is already a compressed scale).

    :param bins: Spectrum magnitudes, one per requested band.
    :param gain_db: Gain to apply before scaling (see _dbu16_to_amplitude).
    """
    scaled = bytes(
        round(_dbu16_to_amplitude(bin_value, gain_db) * 255.0) for bin_value in bins[:_FFT_BINS]
    )
    if len(scaled) < _FFT_BINS:
        scaled += bytes(_FFT_BINS - len(scaled))
    return scaled


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
