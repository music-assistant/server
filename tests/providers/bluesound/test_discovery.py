"""Tests for discovery of the secondary zones of multi-zone BluOS devices."""

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
    provider.mass.players.get_player.return_value = None
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

    _, player_id, discovery_info, *_ = player_cls.call_args.args
    assert player_id == "00:11:22:aa:bb:cc:11010"
    assert discovery_info["mac"] == "00:11:22:aa:bb:cc"
