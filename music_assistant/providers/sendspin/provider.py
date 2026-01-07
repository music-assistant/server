"""Player Provider for Sendspin."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from aiosendspin.server import ClientAddedEvent, ClientRemovedEvent, SendspinEvent, SendspinServer
from music_assistant_models.enums import ProviderFeature

from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.sendspin.player import SendspinPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.providers.hass import HomeAssistantProvider


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
                if player.device_info.manufacturer == "ESPHome" and (
                    hass := self.mass.get_provider("hass")
                ):
                    # Try to get device name from Home Assistant for ESPHome devices
                    hass = cast("HomeAssistantProvider", hass)
                    if hass_device := await hass.get_device_by_connection(client_id):
                        player._attr_name = (
                            hass_device["name_by_user"] or hass_device["name"] or player.name
                        )
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
            advertise_addresses=[cast("str", self.mass.streams.publish_ip)],
        )

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).

        :param is_removed: True when the provider is removed from the configuration.
        """
        # Disconnect all clients before stopping the server
        clients = list(self.server_api.clients)
        disconnect_tasks = []
        for client in clients:
            self.logger.debug("Disconnecting client %s", client.client_id)
            disconnect_tasks.append(client.disconnect(retry_connection=False))
        if disconnect_tasks:
            results = await asyncio.gather(*disconnect_tasks, return_exceptions=True)
            for client, result in zip(clients, results, strict=True):
                if isinstance(result, Exception):
                    self.logger.warning(
                        "Error disconnecting client %s: %s", client.client_id, result
                    )

        # Stop the Sendspin server
        await self.server_api.close()

        for cb in self.unregister_cbs:
            cb()
        self.unregister_cbs = []
