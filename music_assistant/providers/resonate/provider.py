"""Player Provider for Resonate."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from aioresonate.server import ResonateServer
from zeroconf import ServiceStateChange

from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest
    from zeroconf.asyncio import AsyncServiceInfo


class ResonateProvider(PlayerProvider):
    """Player Provider for Resonate."""

    server: ResonateServer

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize a new Resonate player provider."""
        super().__init__(mass, manifest, config)
        self.server = ResonateServer(self.mass.loop)

    @property
    @override
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return set()

    @override
    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        for player in self.players:
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    @override
    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if state_change == ServiceStateChange.Removed:
            # we don't listen for removed players here.
            # instead we just wait for the player connection to fail
            return
        if not info:
            return  # guard

        name = name.split("@", 1)[1] if "@" in name else name
        if path := info.properties.get(b"path"):
            ip = get_primary_ip_address_from_zeroconf(info)
            assert ip
            url = "ws://" + ip + ":" + str(get_port_from_zeroconf(info)) + path.decode()

            self.logger.debug("Discovered resonate player, connecting to %s", url)
            _ = await self.server.connect_to_player(url)
        # player_id = info.decoded_properties["player_id"]
        # TODO add player discovery handling here
