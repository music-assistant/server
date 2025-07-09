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

    bluos_players: dict[str, BluesoundPlayer] = {}

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {ProviderFeature.SYNC_PLAYERS}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback for BluOS."""
        name = name.split(".", 1)[0]
        assert info is not None
        player_id = info.decoded_properties["mac"]
        assert player_id is not None

        # Handle removed player
        if state_change == ServiceStateChange.Removed:
            # Check if the player manager has an existing entry for this player
            if mass_player := self.mass.players.get(player_id):
                # The player has become unavailable
                self.logger.debug("Player offline: %s", mass_player.display_name)
                mass_player._attr_available = False
                mass_player.update_state()
            return

        ip_address = get_primary_ip_address_from_zeroconf(info)
        port = get_port_from_zeroconf(info)

        assert ip_address is not None
        assert port is not None

        # Handle update of existing player
        if bluos_player := self.bluos_players.get(player_id):
            bluos_player.connected = True
            if mass_player := self.mass.players.get(player_id):
                mass_player.available = True
                self.mass.players.update(player_id)
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
        self.bluos_players[player_id] = bluos_player

        # Register with Music Assistant
        await bluos_player.setup()
