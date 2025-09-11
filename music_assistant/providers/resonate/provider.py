"""Player Provider for Resonate."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from aioresonate.server import PlayerAddedEvent, PlayerRemovedEvent, ResonateEvent, ResonateServer
from music_assistant_models.enums import ProviderFeature
from zeroconf import ServiceStateChange

from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.resonate.player import ResonatePlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from zeroconf.asyncio import AsyncServiceInfo


class ResonateProvider(PlayerProvider):
    """Player Provider for Resonate."""

    server_api: ResonateServer
    unregister_cbs: list[Callable[[], None]]

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize a new Resonate player provider."""
        super().__init__(mass, manifest, config)
        self.server_api = ResonateServer(
            self.mass.loop, mass.server_id, "Music Assistant", self.mass.http_session
        )
        self.unregister_cbs = [
            self.server_api.add_event_listener(self.event_cb),
            # For the web player
            self.mass.webserver.register_dynamic_route(
                "/resonate", self.server_api.on_player_connect
            ),
        ]

    async def event_cb(self, event: ResonateEvent) -> None:
        """Event callback registered to the resonate server."""
        self.logger.debug("Received ResonateEvent: %s", event)
        match event:
            case PlayerAddedEvent(player_id):
                player = ResonatePlayer(self, player_id)
                self.logger.info("Player %s connected", player_id)
                await self.mass.players.register(player)
            case PlayerRemovedEvent(player_id):
                self.logger.info("Player %s disconnected", player_id)
                await self.mass.players.unregister(player_id)
            case _:
                self.logger.error("Unknown resonate event: %s", event)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SYNC_PLAYERS,
            ProviderFeature.CREATE_GROUP_PLAYER,
            ProviderFeature.REMOVE_GROUP_PLAYER,
        }

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # Start server for handling incoming Resonate connections on
        # /resonate on the default port
        await self.server_api.start_server(port=8927)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        # Stop the Resonate server
        await self.server_api.close()

        for cb in self.unregister_cbs:
            cb()
        self.unregister_cbs = []
        for player in self.players:
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        self.logger.debug(
            "MDNS service state change for resonate, name=%s, state_change=%s, info=%s",
            name,
            state_change,
            info,
        )
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
            self.server_api.connect_to_player(url)
