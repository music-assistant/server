"""Unit tests for AirPlay provider helpers."""

from unittest.mock import MagicMock

import pytest

from music_assistant.providers.airplay.helpers import (
    serialize_txt_records,
    supports_airplay2,
)


@pytest.mark.parametrize(
    ("features_value", "expected"),
    [
        # Apple TV 4K: UnifiedMediaControl (38) and CoreUtils (48) set
        ("0x4A7FDFD5,0x3C177FDE", True),
        # Sonos: UnifiedMediaControl set
        ("0x445F8A00,0x1C340", True),
        # legacy RAOP-only receiver (single low field, bits 38/48 unreachable)
        ("0x5A7FFFF7", False),
        # high field present but neither AirPlay 2 bit set
        ("0xFFFFFFFF,0x2", False),
        # bit 48 (CoreUtils) alone is enough
        ("0x0,0x10000", True),
        # missing/garbage values never claim support
        (None, False),
        ("", False),
        ("not-a-number", False),
    ],
)
def test_supports_airplay2(features_value: str | None, expected: bool) -> None:
    """AirPlay 2 support is detected from the UnifiedMediaControl/CoreUtils feature bits."""
    assert supports_airplay2(features_value) is expected


def test_serialize_txt_records() -> None:
    """TXT records serialize to space-separated k=v pairs, skipping unsafe entries."""
    discovery_info = MagicMock()
    discovery_info.decoded_properties = {
        "features": "0x5A7FFFF7,0x1E",
        "flags": "0x4",
        "deviceid": "AA:BB:CC:DD:EE:FF",
        "manufacturer": "Acme, Inc.",  # value contains a space: skipped
        "odd key": "value",  # key contains a space: skipped
        "empty": None,  # zeroconf may decode a valueless entry as None: skipped
    }
    txt = serialize_txt_records(discovery_info)
    pairs = txt.split(" ")
    assert "features=0x5A7FFFF7,0x1E" in pairs
    assert "flags=0x4" in pairs
    assert "deviceid=AA:BB:CC:DD:EE:FF" in pairs
    assert len(pairs) == 3
