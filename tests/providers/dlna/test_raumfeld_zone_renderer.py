"""Tests for skipping the virtual zone renderers a Teufel Raumfeld host publishes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider, use_player_registry

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


def test_zone_renderer_with_legacy_manufacturer_is_recognised() -> None:
    """Older Raumfeld firmware reports "Raumfeld GmbH" as the manufacturer."""
    assert _player("Raumfeld GmbH", AV_TRANSPORT)._is_raumfeld_zone_renderer() is True


def test_speaker_renderer_is_kept() -> None:
    """A Raumfeld speaker's own renderer carries the RaumfeldGenerator service."""
    player = _player(TEUFEL, AV_TRANSPORT, RAUMFELD_GENERATOR)
    assert player._is_raumfeld_zone_renderer() is False


def test_other_manufacturers_are_kept() -> None:
    """Only Teufel devices are checked; others never carry that service."""
    assert _player("Some Other Brand", AV_TRANSPORT)._is_raumfeld_zone_renderer() is False


async def test_setup_ignores_a_zone_renderer() -> None:
    """Setup skips a zone renderer instead of attaching it as a protocol."""
    player = _player(TEUFEL, AV_TRANSPORT)
    attach = AsyncMock()
    disconnect = AsyncMock()
    remove_protocol = AsyncMock()
    record = {"player_id": "pl0123456789ab", "provider": "dlna_test", "enabled": True}
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(player, "_device_disconnect", disconnect),
        patch.object(player.mass.config, "get_protocol_record", MagicMock(return_value=record)),
        patch.object(player.mass.players, "register", attach),
        patch.object(player.mass.players, "remove_protocol", remove_protocol),
    ):
        assert await player.setup() is False
    attach.assert_not_called()
    # it is never attached, so never detached: its event subscriptions end here
    disconnect.assert_awaited_once()
    # its stored protocol goes too, so an existing install stops restoring it as a player
    remove_protocol.assert_awaited_once_with("uuid:dlna-player")


async def test_setup_ignores_a_zone_renderer_without_a_stored_config() -> None:
    """A zone renderer with no stored config has nothing to clean up."""
    player = _player(TEUFEL, AV_TRANSPORT)
    remove_protocol = AsyncMock()
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(player, "_device_disconnect", AsyncMock()),
        patch.object(player.mass.config, "get_protocol_record", MagicMock(return_value=None)),
        patch.object(player.mass.players, "remove_protocol", remove_protocol),
    ):
        assert await player.setup() is False
    remove_protocol.assert_not_called()


async def test_setup_keeps_a_speaker_renderer_config() -> None:
    """A speaker's own renderer is attached as usual and its config is left alone."""
    player = _player(TEUFEL, AV_TRANSPORT, RAUMFELD_GENERATOR)
    registry = use_player_registry(player.mass)._protocols
    attach = AsyncMock(
        side_effect=lambda protocol: registry.update({protocol.protocol_id: protocol})
    )
    remove_protocol = AsyncMock()
    disconnect = AsyncMock()
    with (
        patch.object(player, "_device_connect", AsyncMock()),
        patch.object(player, "_device_disconnect", disconnect),
        patch.object(player, "set_static_attributes", MagicMock()),
        patch.object(player.mass.players, "register", attach),
        patch.object(player.mass.players, "remove_protocol", remove_protocol),
    ):
        assert await player.setup() is True
    attach.assert_awaited_once_with(player)
    remove_protocol.assert_not_called()
    disconnect.assert_not_awaited()
