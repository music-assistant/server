"""Tests for the Web Radio Broadcast helper functions."""

from __future__ import annotations

import pytest

from music_assistant.providers.webradio.helpers import slugify_station_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Rock Radio", "rock-radio"),
        ("Jazz / Blues 24/7", "jazz-blues-24-7"),
        ("Café", "cafe"),
        ("   spaces   ", "spaces"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_slugify_station_name(name: str, expected: str) -> None:
    """slugify_station_name produces URL-safe lowercase slugs."""
    assert slugify_station_name(name) == expected
