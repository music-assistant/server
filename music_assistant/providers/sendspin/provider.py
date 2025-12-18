"""Player Provider for Sendspin."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from aiosendspin.server import ClientAddedEvent, ClientRemovedEvent, SendspinEvent, SendspinServer
from music_assistant_models.enums import ProviderFeature

from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.sendspin.player import SendspinPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


class SendspinProvider(PlayerProvider):
    """Player Provider for Sendspin."""

    server_api: SendspinServer
    unregister_cbs: list[Callable[[], None]]
    _pending_unregisters: dict[str, asyncio.Event]

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize a new Sendspin player provider."""
        super().__init__(mass, manifest, config)
        self.server_api = SendspinServer(
            self.mass.loop, mass.server_id, "Music Assistant", self.mass.http_session
        )
        self._pending_unregisters = {}
        self.unregister_cbs = [
            self.server_api.add_event_listener(self.event_cb),
            self.mass.register_api_command(
                "sendspin/connection_info", self.handle_get_connection_info
            ),
        ]

    async def event_cb(self, server: SendspinServer, event: SendspinEvent) -> None:
        """Event callback registered to the sendspin server."""
        self.logger.debug("Received SendspinEvent: %s", event)
        match event:
            case ClientAddedEvent(client_id):
                # Wait for any pending unregister to complete before registering
                # This prevents a race condition where a slow unregister removes
                # a newly registered player after a quick reconnect
                if pending_event := self._pending_unregisters.get(client_id):
                    self.logger.debug(
                        "Waiting for pending unregister of %s before registering", client_id
                    )
                    await pending_event.wait()
                player = SendspinPlayer(self, client_id)
                self.logger.debug("Client %s connected", client_id)
                await self.mass.players.register(player)
            case ClientRemovedEvent(client_id):
                self.logger.debug("Client %s disconnected", client_id)
                unregister_event = asyncio.Event()
                self._pending_unregisters[client_id] = unregister_event
                try:
                    await self.mass.players.unregister(client_id)
                finally:
                    self._pending_unregisters.pop(client_id, None)
                    unregister_event.set()
            case _:
                self.logger.error("Unknown sendspin event: %s", event)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SYNC_PLAYERS,
        }

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        # Start server for handling incoming Sendspin connections from clients
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

        :param is_removed: True when the provider is removed from the configuration.
        """
        # Stop the Sendspin server
        await self.server_api.close()

        for cb in self.unregister_cbs:
            cb()
        self.unregister_cbs = []
        for player in self.players:
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    async def handle_get_connection_info(self, client_id: str) -> dict[str, Any]:
        """
        Get sendspin connection info.

        This command auto-whitelists the player for users with player filters enabled,
        allowing them to use the web player, and returns the WebSocket URL for connecting.

        :param client_id: The sendspin client ID.
        :return: Dictionary with ws_url for connecting to the sendspin proxy.
        """
        if user := get_current_user():
            if user.player_filter and client_id not in user.player_filter:
                self.logger.debug(
                    "Auto-whitelisting Sendspin player %s for user %s",
                    client_id,
                    user.username,
                )
                new_filter = [*user.player_filter, client_id]
                await self.mass.webserver.auth.update_user_filters(
                    user, player_filter=new_filter, provider_filter=None
                )
                user.player_filter = new_filter

        base_url = self.mass.webserver.base_url
        parsed = urlparse(base_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_url = f"{ws_scheme}://{parsed.netloc}/sendspin"

        return {"ws_url": ws_url}
