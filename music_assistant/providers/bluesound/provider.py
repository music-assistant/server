"""Bluesound Player Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

from zeroconf import ServiceStateChange

from music_assistant.helpers.util import (
    get_mac_address,
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

from .const import MUSP_MDNS_TYPE
from .player import BluesoundPlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


class BluesoundDiscoveryInfo(TypedDict):
    """Template for MDNS discovery info."""

    _objectType: str
    ip_address: str
    port: str
    mac: str
    model: str
    zs: bool


class BluesoundPlayerProvider(PlayerProvider):
    """Bluos compatible player provider, providing support for bluesound speakers."""

    player_map: dict[(str, str), str] = {}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback for BluOS."""
        if state_change == ServiceStateChange.Removed:
            # Wait for connection to fail, same as sonos.
            return
        name = name.split(".", 1)[0]
        assert info is not None

        ip_address = get_primary_ip_address_from_zeroconf(info)
        port = get_port_from_zeroconf(info)

        if not ip_address or not port:
            self.logger.debug("Ignoring incomplete mdns discovery for Bluesound player: %s", name)
            return

        if info.type == MUSP_MDNS_TYPE:
            # this is a multi-zone device, we need to fetch the mac address of the main device
            mac_address = await get_mac_address(ip_address)
            player_id = f"{mac_address}:{port}"
        else:
            mac_address = info.decoded_properties.get("mac")
            player_id = mac_address

        if not mac_address:
            self.logger.debug(
                "Ignoring mdns discovery for Bluesound player without MAC address: %s",
                name,
            )
            return

        # Handle update of existing player
        assert player_id is not None  # for type checker
        if bluos_player := self.mass.players.get(player_id):
            bluos_player = cast("BluesoundPlayer", bluos_player)
            # Check if the IP address has changed
            if ip_address and ip_address != bluos_player.ip_address:
                self.logger.debug(
                    "IP address for player %s updated to %s", bluos_player.name, ip_address
                )
            else:
                # IP address not changed
                self.logger.debug("Player back online: %s", bluos_player.name)
                bluos_player._attr_available = True
                await bluos_player.update_attributes()
                return

        # New player discovered
        self.logger.debug("Discovered player: %s", name)

        discovery_info = BluesoundDiscoveryInfo(
            _objectType=info.decoded_properties.get("_objectType", ""),
            ip_address=ip_address,
            port=str(port),
            mac=mac_address,
            model=info.decoded_properties.get("model", ""),
            zs=info.decoded_properties.get("zs", False),
        )

        # Create BluOS player
        bluos_player = BluesoundPlayer(self, player_id, discovery_info, name, ip_address, port)
        self.player_map[(ip_address, port)] = player_id

        # Register with Music Assistant
        await bluos_player.setup()
