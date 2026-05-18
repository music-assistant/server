"""Byte-golden tests for the WLED V2 audio-sync packet encoder."""

from __future__ import annotations

import struct

import pytest

from music_assistant.providers.wled_audiosync.constants import (
    WLED_V2_MAGIC_HEADER,
    WLED_V2_PACKET_SIZE,
)
from music_assistant.providers.wled_audiosync.wled_audiosync_bridge.encoder import (
    V2_STRUCT_FORMAT,
    WledV2Frame,
    encode_v2,
)

# First distinct frame captured from a real MoonModules ESP32 sender
# (source MAC 20:e7:c8:6a:c5:40, multicast to 239.0.0.1:11988).
# Reference capture: wled_v2_reference.pcap (700 packets / 350 distinct frames).
# See docs/wled_audiosync_design.md §5 for the field-by-field walkthrough.
PCAP_PACKET_1_HEX = (
    "303030303200000000007e43539d694301009f785e977153b5e6dc480000000000000000083736461ee5ef42"
)


def test_v2_struct_format_yields_44_bytes() -> None:
    """The struct format must pack to exactly 44 bytes (the on-wire size)."""
    assert struct.calcsize(V2_STRUCT_FORMAT) == WLED_V2_PACKET_SIZE


def test_encoder_matches_real_capture_byte_for_byte() -> None:
    """encode_v2 reproduces a real WLED MM packet byte-for-byte."""
    expected = bytes.fromhex(PCAP_PACKET_1_HEX)
    assert len(expected) == WLED_V2_PACKET_SIZE

    # Decode the captured packet so the test inputs are exact IEEE 754 values.
    assert expected[:6] == WLED_V2_MAGIC_HEADER
    assert expected[6:8] == b"\x00\x00"  # alignment padding
    sample_raw, sample_smth = struct.unpack("<2f", expected[8:16])
    sample_peak = expected[16]
    reserved1 = expected[17]
    fft_bands = expected[18:34]
    assert expected[34:36] == b"\x00\x00"  # alignment padding
    fft_magnitude, fft_major_peak_hz = struct.unpack("<2f", expected[36:44])

    encoded = encode_v2(
        WledV2Frame(
            sample_raw=sample_raw,
            sample_smth=sample_smth,
            sample_peak=sample_peak,
            fft_bands=fft_bands,
            fft_magnitude=fft_magnitude,
            fft_major_peak_hz=fft_major_peak_hz,
        )
    )

    assert encoded == expected
    assert reserved1 == 0  # capture invariant — encoder always writes 0


def test_encoder_zeroes_padding_regions() -> None:
    """The two compiler-alignment padding regions must always be zero on the wire."""
    encoded = encode_v2(
        WledV2Frame(
            sample_raw=0.0,
            sample_smth=0.0,
            sample_peak=0,
            fft_bands=bytes(16),
            fft_magnitude=0.0,
            fft_major_peak_hz=0.0,
        )
    )
    assert encoded[6:8] == b"\x00\x00"
    assert encoded[34:36] == b"\x00\x00"


def test_encoder_uses_v2_magic_header() -> None:
    """Every encoded packet starts with the V2 magic header."""
    encoded = encode_v2(
        WledV2Frame(
            sample_raw=1.0,
            sample_smth=2.0,
            sample_peak=1,
            fft_bands=bytes(range(16)),
            fft_magnitude=3.0,
            fft_major_peak_hz=440.0,
        )
    )
    assert encoded[:6] == WLED_V2_MAGIC_HEADER == b"00002\x00"


def test_encoder_rejects_wrong_band_count() -> None:
    """Passing a fft_bands buffer that is not 16 bytes is an encoder bug."""
    with pytest.raises(ValueError, match="fft_bands must be exactly 16 bytes"):
        encode_v2(
            WledV2Frame(
                sample_raw=0.0,
                sample_smth=0.0,
                sample_peak=0,
                fft_bands=b"\x00" * 15,
                fft_magnitude=0.0,
                fft_major_peak_hz=0.0,
            )
        )


def test_encoder_masks_sample_peak_to_uint8() -> None:
    """sample_peak is a uint8 on the wire; encoder masks higher bits."""
    encoded = encode_v2(
        WledV2Frame(
            sample_raw=0.0,
            sample_smth=0.0,
            sample_peak=0x101,  # extra bits should be discarded
            fft_bands=bytes(16),
            fft_magnitude=0.0,
            fft_major_peak_hz=0.0,
        )
    )
    assert encoded[16] == 0x01
