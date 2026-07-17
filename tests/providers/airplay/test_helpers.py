"""Unit tests for AirPlay provider helpers."""

import pytest

from music_assistant.providers.airplay.helpers import is_airplay2_preferred_model


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
