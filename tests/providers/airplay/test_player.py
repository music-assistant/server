"""Unit tests for AirPlay player."""

from unittest.mock import MagicMock

import pytest

from music_assistant.providers.airplay.player import AirPlayPlayer


@pytest.mark.parametrize(
    ("aiplay_properties", "raop_properties", "expected"),
    [
        ({b"flags": b"0x200"}, None, True),
        ({b"sf": b"0x201"}, None, True),
        ({b"flags": b"0x4"}, None, False),
        ({b"sf": b"0x8"}, None, True),
        ({b"flags": b"0x9"}, None, True),
        (None, {b"flags": "0x200"}, True),
        (None, {b"sf": b"0x201"}, True),
        (None, {b"flags": b"0x4"}, False),
        (None, {b"sf": b"0x8"}, True),
        (None, {b"flags": b"0x9"}, True),
        ({}, {}, False),
    ],
)
def test_requires_pairing(
    aiplay_properties: dict[bytes, bytes] | None,
    raop_properties: dict[bytes, bytes] | None,
    expected: bool,
) -> None:
    """Test the _requires_pairing method of AirPlayPlayer."""
    if aiplay_properties is not None:
        aiplay_discovery_info = MagicMock()
        aiplay_discovery_info.properties = aiplay_properties
    else:
        aiplay_discovery_info = None
    if raop_properties is not None:
        raop_discovery_info = MagicMock()
        raop_discovery_info.properties = raop_properties
    else:
        raop_discovery_info = None
    player = AirPlayPlayer(
        provider=MagicMock(),
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=raop_discovery_info,
        airplay_discovery_info=aiplay_discovery_info,
    )
    assert player._requires_pairing() == expected
