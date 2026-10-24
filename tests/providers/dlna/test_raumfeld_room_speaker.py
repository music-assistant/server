"""Tests for leaving out the speakers a Teufel Raumfeld room plays along with its own."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider

TEUFEL = "Lautsprecher Teufel GmbH"
SPEAKER_UDN = "uuid:one-s"


def _player(manufacturer: str = TEUFEL, played_along: bool | None = True) -> DLNAPlayer:
    """
    Return the player of a speaker's own renderer.

    :param manufacturer: The manufacturer the device reports.
    :param played_along: Whether the Raumfeld provider plays the speaker along with its
        room, or None when no Raumfeld provider is set up.
    """
    device = MagicMock()
    device.name = "Speaker Living"
    device.manufacturer = manufacturer
    device.has_play_media = True
    device.profile_device.root_device.all_services = [
        MagicMock(service_type="urn:schemas-raumfeld-com:service:RaumfeldGenerator:1")
    ]
    provider = MockProvider("dlna", instance_id="dlna_test")
    if played_along is None:
        provider.mass.get_provider.return_value = None
    else:
        provider.mass.get_provider.return_value.is_other_room_speaker.return_value = played_along
    return DLNAPlayer(
        provider,  # type: ignore[arg-type]
        SPEAKER_UDN,
        "http://192.168.1.21/description.xml",
        device=device,
    )


def test_a_speaker_played_along_with_its_room_is_recognised() -> None:
    """The Raumfeld provider is asked about the speaker's own renderer."""
    player = _player()
    assert player._is_other_raumfeld_room_speaker() is True
    get_provider = cast("MagicMock", player.mass.get_provider)
    get_provider.assert_called_once_with("raumfeld")
    get_provider.return_value.is_other_room_speaker.assert_called_once_with(SPEAKER_UDN)


def test_a_speaker_is_kept_without_the_raumfeld_provider() -> None:
    """Without the Raumfeld provider, every speaker stays a player of its own."""
    assert _player(played_along=None)._is_other_raumfeld_room_speaker() is False
    assert _player(played_along=False)._is_other_raumfeld_room_speaker() is False


def test_other_manufacturers_are_not_asked_about() -> None:
    """Only Teufel devices can be part of a Raumfeld room."""
    player = _player("Some Other Brand")
    assert player._is_other_raumfeld_room_speaker() is False
    cast("MagicMock", player.mass.get_provider).assert_not_called()


async def test_setup_ignores_a_speaker_played_along_with_its_room() -> None:
    """Setup skips the speaker instead of registering it as a player of its own."""
    player = _player()
    register = AsyncMock()
    disconnect = AsyncMock()
    delete_config = MagicMock()
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(player, "_device_disconnect", disconnect),
        patch.object(player.mass.players, "register_or_update", register),
        patch.object(player.mass.players, "delete_player_config", delete_config),
    ):
        assert await player.setup() is False
    register.assert_not_called()
    disconnect.assert_awaited_once()
    delete_config.assert_called_once_with(SPEAKER_UDN)


async def test_poll_removes_a_speaker_found_to_be_played_along() -> None:
    """A speaker set up before the host named its room is removed once it has."""
    player = _player()
    ignore = MagicMock()
    player.provider.ignore_player = ignore  # type: ignore[attr-defined]
    unregister = AsyncMock()
    with patch.object(player.mass.players, "unregister", unregister):
        await player.poll()
    ignore.assert_called_once_with(SPEAKER_UDN)
    unregister.assert_awaited_once_with(SPEAKER_UDN, permanent=True)
