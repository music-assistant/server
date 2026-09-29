"""Tests for skipping the virtual zone renderers a Teufel Raumfeld host publishes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider

TEUFEL = "Lautsprecher Teufel GmbH"
AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RAUMFELD_GENERATOR = "urn:schemas-raumfeld-com:service:RaumfeldGenerator:1"


def _player(manufacturer: str, *service_types: str) -> DLNAPlayer:
    """
    Return a player whose device reports the given manufacturer and services.

    :param manufacturer: The manufacturer the device reports.
    :param service_types: The service types the device offers.
    """
    device = MagicMock()
    device.name = "Bar"
    device.manufacturer = manufacturer
    device.has_play_media = True
    device.profile_device.root_device.all_services = [
        MagicMock(service_type=service_type) for service_type in service_types
    ]
    return DLNAPlayer(
        MockProvider("dlna", instance_id="dlna_test"),  # type: ignore[arg-type]
        "uuid:dlna-player",
        "http://192.168.1.41/description.xml",
        device=device,
    )


def test_zone_renderer_is_recognised() -> None:
    """A Teufel renderer without the RaumfeldGenerator service is a host's zone renderer."""
    assert _player(TEUFEL, AV_TRANSPORT)._is_raumfeld_zone_renderer() is True


def test_speaker_renderer_is_kept() -> None:
    """A Raumfeld speaker's own renderer carries the RaumfeldGenerator service."""
    player = _player(TEUFEL, AV_TRANSPORT, RAUMFELD_GENERATOR)
    assert player._is_raumfeld_zone_renderer() is False


def test_other_manufacturers_are_kept() -> None:
    """Only Teufel devices are checked; others never carry that service."""
    assert _player("Some Other Brand", AV_TRANSPORT)._is_raumfeld_zone_renderer() is False


async def test_setup_ignores_a_zone_renderer() -> None:
    """Setup skips a zone renderer instead of registering it as a player."""
    player = _player(TEUFEL, AV_TRANSPORT)
    register = AsyncMock()
    delete_config = MagicMock()
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(player.mass.players, "register_or_update", register),
        patch.object(player.mass.players, "delete_player_config", delete_config),
    ):
        assert await player.setup() is False
    register.assert_not_called()
    # its stored config goes too, so an existing install stops restoring it as a player
    delete_config.assert_called_once_with("uuid:dlna-player")


async def test_setup_survives_a_failing_config_removal() -> None:
    """Removing the config is best effort and never breaks discovery."""
    player = _player(TEUFEL, AV_TRANSPORT)
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(
            player.mass.players, "delete_player_config", MagicMock(side_effect=KeyError("x"))
        ),
    ):
        assert await player.setup() is False


async def test_setup_keeps_a_speaker_renderer_config() -> None:
    """A speaker's own renderer is set up as usual and its config is left alone."""
    player = _player(TEUFEL, AV_TRANSPORT, RAUMFELD_GENERATOR)
    delete_config = MagicMock()
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(player, "set_static_attributes", MagicMock()),
        patch.object(player.mass.players, "register_or_update", AsyncMock()),
        patch.object(player.mass.players, "delete_player_config", delete_config),
    ):
        assert await player.setup() is True
    delete_config.assert_not_called()
