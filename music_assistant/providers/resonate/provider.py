"""Player Provider for Resonate."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from aioresonate.server import ClientAddedEvent, ClientRemovedEvent, ResonateEvent, ResonateServer
from music_assistant_models.enums import ProviderFeature

from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.resonate.player import ResonatePlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


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
                "/resonate", self.server_api.on_client_connect
            ),
        ]

    async def event_cb(self, event: ResonateEvent) -> None:
        """Event callback registered to the resonate server."""
        self.logger.debug("Received ResonateEvent: %s", event)
        match event:
            case ClientAddedEvent(client_id):
                player = ResonatePlayer(self, client_id)
                self.logger.debug("Client %s connected", client_id)
                await self.mass.players.register(player)
            case ClientRemovedEvent(client_id):
                self.logger.debug("Client %s disconnected", client_id)
                await self.mass.players.unregister(client_id)
            case _:
                self.logger.error("Unknown resonate event: %s", event)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SYNC_PLAYERS,
        }

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # Start server for handling incoming Resonate connections from clients
        # and mDNS discovery of new clients
        await self.server_api.start_server(
            port=8927,
            host=self.mass.streams.bind_ip,
            advertise_host=cast("str", self.mass.streams.publish_ip),
        )

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
