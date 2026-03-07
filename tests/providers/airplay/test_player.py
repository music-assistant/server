"""Unit tests for AirPlay player."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.airplay.constants import StreamingProtocol
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
def test_requires_pin_pairing(
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
    assert player._requires_pin_pairing() == expected


@pytest.mark.parametrize(
    ("aiplay_properties", "raop_properties", "expected"),
    [
        ({b"flags": b"0x80"}, None, True),
        ({b"sf": b"0x81"}, None, True),
        ({b"flags": b"0x4"}, None, False),
        ({b"sf": b"0x80"}, None, True),
        ({b"flags": b"0x90"}, None, True),
        (None, {b"flags": "0x80"}, True),
        (None, {b"sf": b"0x81"}, True),
        (None, {b"flags": b"0x4"}, False),
        (None, {b"sf": b"0x80"}, True),
        (None, {b"flags": b"0x90"}, True),
        ({}, {}, False),
    ],
)
def test_requires_password_pairing(
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
    assert player._requires_password_pairing() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flags", "pin_call_expected"),
    [
        (b"0x8", True),
        (b"0x80", False),
    ],
)
async def test_start_pairing__pin_decision(flags: bytes, pin_call_expected: bool) -> None:
    """Ensure _start_pairing skips the PIN request when only password pairing is required."""
    aiplay_info = MagicMock()
    aiplay_info.properties = {b"flags": flags}
    aiplay_info.port = 7000

    provider = MagicMock()
    provider.dacp_id = "test_dacp"

    player = AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=None,
        airplay_discovery_info=aiplay_info,
    )

    pairing_instance = AsyncMock()
    pairing_instance.start_pairing_session = AsyncMock()
    pairing_instance.start_pin_pairing = AsyncMock()

    with patch(
        "music_assistant.providers.airplay.pairing.AirPlayPairing",
        return_value=pairing_instance,
    ):
        await player._start_pairing(StreamingProtocol.AIRPLAY2, "AirPlay2")

    pairing_instance.start_pairing_session.assert_called_once()
    if pin_call_expected:
        pairing_instance.start_pin_pairing.assert_called_once()
    else:
        pairing_instance.start_pin_pairing.assert_not_called()
