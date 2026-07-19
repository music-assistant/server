"""Unit tests for AirPlay provider helpers."""

from unittest.mock import MagicMock

import pytest

from music_assistant.providers.airplay.helpers import (
    is_airplay2_preferred_model,
    serialize_txt_records,
)


@pytest.mark.parametrize(
    ("manufacturer", "model", "expected"),
    [
        # existing exact entries keep working
        ("Ubiquiti Inc.", "UPL-AMP", True),
        ("LG Electronics", "Some Soundbar", True),
        # known JBL models, matched case-insensitively on the am-derived model name
        ("AirPlay", "JBL BAR 1300", True),
        ("AirPlay", "JBL BAR 300", True),
        ("AirPlay", "JBL Charge 5 Wi-Fi", True),
        ("AirPlay", "jbl bar 1300", True),
        # no match for unrelated or unlisted devices
        ("Sonos", "One", False),
        ("AirPlay", "Unknown", False),
        ("AirPlay", "JBL Flip 6", False),
    ],
)
def test_is_airplay2_preferred_model(manufacturer: str, model: str, expected: bool) -> None:
    """Matching is case-insensitive and supports fnmatch-style wildcards."""
    assert is_airplay2_preferred_model(manufacturer, model) is expected


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
