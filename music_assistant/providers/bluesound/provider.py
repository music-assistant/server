"""Bluesound Player Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from music_assistant_models.enums import ProviderFeature
from zeroconf import ServiceStateChange

from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

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

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SYNC_PLAYERS,
            ProviderFeature.CREATE_GROUP_PLAYER,
            ProviderFeature.REMOVE_GROUP_PLAYER,
        }

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
        player_id = info.decoded_properties["mac"]
        assert player_id is not None

        ip_address = get_primary_ip_address_from_zeroconf(info)
        port = get_port_from_zeroconf(info)

        assert ip_address is not None
        assert port is not None

        # Handle update of existing player
        if bluos_player := self.mass.players.get(player_id):
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
            mac=info.decoded_properties["mac"],
            model=info.decoded_properties.get("model", ""),
            zs=info.decoded_properties.get("zs", False),
        )

        # Create BluOS player
        bluos_player = BluesoundPlayer(self, player_id, discovery_info, name, ip_address, port)
        self.player_map[(ip_address, port)] = player_id

        # Register with Music Assistant
        await bluos_player.setup()
