"""WLED V2 audio-sync UDP packet encoder.

The 44-byte wire format was validated byte-for-byte against a real-hardware
MoonModules capture during Phase 0 (see docs/wled_audiosync_design.md §5).
The struct is naturally aligned (NOT packed): there are 2 zero bytes of
alignment padding after the 6-byte header and another 2 zero bytes after
the 16-byte fftResult array. All multi-byte fields are little-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .constants import WLED_V2_MAGIC_HEADER, WLED_V2_PACKET_SIZE

# struct.pack format yielding exactly 44 bytes:
#   6s  header[6]            -> "00002\0"
#   2s  _pad                 -> b"\0\0" (alignment)
#   ff  sampleRaw, sampleSmth
#   2B  samplePeak, reserved1
#   16s fftResult[16]
#   2s  _pad                 -> b"\0\0" (alignment)
#   ff  FFT_Magnitude, FFT_MajorPeak
V2_STRUCT_FORMAT = "<6s2sff2B16s2sff"

# Internal padding sentinels — kept named so the layout stays self-documenting.
_PAD2 = b"\x00\x00"

_FFT_BAND_COUNT = 16


@dataclass(frozen=True)
class WledV2Frame:
    """
    One frame of WLED V2 audio-sync data, ready to encode.

    :param sample_raw: AGC-scaled raw amplitude (0.0-255.0 per capture).
    :param sample_smth: Smoothed amplitude (0.0-255.0 per capture).
    :param sample_peak: Beat-detected flag, 0 or 1.
    :param fft_bands: 16 GEQ band magnitudes, each in 0-255. Must be 16 bytes.
    :param fft_magnitude: Magnitude of the dominant FFT peak (arbitrary scale).
    :param fft_major_peak_hz: Dominant peak frequency in Hz.
    """

    sample_raw: float
    sample_smth: float
    sample_peak: int
    fft_bands: bytes
    fft_magnitude: float
    fft_major_peak_hz: float


def encode_v2(frame: WledV2Frame) -> bytes:
    """Pack a WledV2Frame into the 44-byte wire-format UDP payload."""
    if len(frame.fft_bands) != _FFT_BAND_COUNT:
        msg = f"fft_bands must be exactly {_FFT_BAND_COUNT} bytes, got {len(frame.fft_bands)}"
        raise ValueError(msg)
    packet = struct.pack(
        V2_STRUCT_FORMAT,
        WLED_V2_MAGIC_HEADER,
        _PAD2,
        frame.sample_raw,
        frame.sample_smth,
        frame.sample_peak & 0xFF,
        0,  # reserved1
        frame.fft_bands,
        _PAD2,
        frame.fft_magnitude,
        frame.fft_major_peak_hz,
    )
    # Defence in depth — should never trigger given the format string above.
    if len(packet) != WLED_V2_PACKET_SIZE:
        msg = f"encoded packet size mismatch: {len(packet)} != {WLED_V2_PACKET_SIZE}"
        raise AssertionError(msg)
    return packet
