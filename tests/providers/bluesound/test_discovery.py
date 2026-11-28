"""Tests for mDNS discovery of BluOS players."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from zeroconf import ServiceStateChange

from music_assistant.providers.bluesound.const import MUSP_MDNS_TYPE
from music_assistant.providers.bluesound.provider import BluesoundPlayerProvider

MODULE = "music_assistant.providers.bluesound.provider"


async def test_musp_zone_falls_back_to_sync_status_mac_without_arp() -> None:
    """A secondary zone must still register when ARP can't resolve the chassis MAC."""
    provider = BluesoundPlayerProvider.__new__(BluesoundPlayerProvider)
    provider.mass = MagicMock()
    provider.mass.players.get_protocol.return_value = None
    provider.mass.players.wants_protocol.return_value = True
    provider.logger = MagicMock()
    info = MagicMock(type=MUSP_MDNS_TYPE, decoded_properties={})
    client = MagicMock()
    client.sync_status = AsyncMock(return_value=MagicMock(mac="00:11:22:AA:BB:CC:11010"))

    with (
        patch(f"{MODULE}.get_primary_ip_address_from_zeroconf", return_value="192.0.2.10"),
        patch(f"{MODULE}.get_port_from_zeroconf", return_value=11010),
        patch(f"{MODULE}.get_mac_address", AsyncMock(return_value=None)),
        patch(f"{MODULE}.BluosPlayer", return_value=client),
        patch(f"{MODULE}.BluesoundPlayer") as player_cls,
    ):
        player_cls.return_value.setup = AsyncMock()
        await provider.on_mdns_service_state_change(
            "Office._musp._tcp.local.", ServiceStateChange.Added, info
        )

    _, protocol_id, discovery_info, *_ = player_cls.call_args.args
    assert protocol_id == "00:11:22:aa:bb:cc:11010"
    assert discovery_info["mac"] == "00:11:22:aa:bb:cc"


async def test_ipv6_only_update_keeps_known_ipv4_player() -> None:
    """An mDNS update with only an IPv6 address must not move a known IPv4 player."""
    provider = BluesoundPlayerProvider.__new__(BluesoundPlayerProvider)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    known_player = MagicMock(ip_address="192.0.2.10")
    known_player.update_attributes = AsyncMock()
    provider.mass.players.get_protocol.return_value = known_player
    info = MagicMock(type="_sum._tcp.local.", decoded_properties={"mac": "00:11:22:aa:bb:cc"})

    with (
        patch(f"{MODULE}.get_primary_ip_address_from_zeroconf", return_value="fd00::1"),
        patch(f"{MODULE}.get_port_from_zeroconf", return_value=11000),
        patch(f"{MODULE}.BluesoundPlayer") as player_cls,
    ):
        await provider.on_mdns_service_state_change(
            "Soundbar._sum._tcp.local.", ServiceStateChange.Updated, info
        )

    player_cls.assert_not_called()
    assert known_player.ip_address == "192.0.2.10"
    known_player.update_attributes.assert_awaited_once()
