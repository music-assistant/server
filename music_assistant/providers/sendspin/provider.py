"""Player Provider for Sendspin."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from aiosendspin.server import ClientAddedEvent, ClientRemovedEvent, SendspinEvent, SendspinServer
from music_assistant_models.enums import IdentifierType, ProviderFeature
from music_assistant_models.errors import AlreadyRegisteredError

from music_assistant.constants import CONF_ENABLED
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
    _bridge_identifiers: dict[str, dict[IdentifierType, str]]
    _client_event_versions: dict[str, int]
    _unloading: bool

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize a new Sendspin player provider."""
        super().__init__(mass, manifest, config)
        self.server_api = SendspinServer(
            self.mass.loop, mass.server_id, "Music Assistant", self.mass.http_session
        )
        self._bridge_identifiers = {}
        self._client_event_versions = {}
        self._unloading = False
        self.unregister_cbs = [
            self.server_api.add_event_listener(self.event_cb),
        ]

    def _next_client_event_version(self, client_id: str) -> int:
        """Increment and return the latest event version for a client id."""
        version = self._client_event_versions.get(client_id, 0) + 1
        self._client_event_versions[client_id] = version
        return version

    def _is_current_client_event(self, client_id: str, event_version: int) -> bool:
        """Return True if the event version is still the latest for the client."""
        return self._client_event_versions.get(client_id) == event_version

    # _handle_client_added and _handle_client_removed are async and run concurrently as tasks.
    # A fast reconnect can produce overlapping tasks: a remove task and an add task both in flight.
    # Each event is assigned a monotonically increasing version; a task checks before acting whether
    # its version is still the latest, and aborts if a newer event has superseded it.

    def event_cb(self, server: SendspinServer, event: SendspinEvent) -> None:
        """Event callback registered to the sendspin server."""
        match event:
            case ClientAddedEvent(client_id):
                event_version = self._next_client_event_version(client_id)
                self.mass.create_task(self._handle_client_added(client_id, event_version))
            case ClientRemovedEvent(client_id):
                event_version = self._next_client_event_version(client_id)
                self.mass.create_task(self._handle_client_removed(client_id, event_version))
            case _:
                self.logger.error("Unknown sendspin event: %s", event)

    def register_bridge_identifiers(
        self, client_id: str, identifiers: dict[IdentifierType, str]
    ) -> None:
        """Pre-register extra identifiers for a bridge client.

        Called by bridge managers (Chromecast, AirPlay) before registering an
        external player, so that the resulting SendspinPlayer carries the parent
        player's protocol-specific identifiers for cross-protocol matching.

        :param client_id: The bridge client_id that will be used for registration.
        :param identifiers: Extra identifiers to attach to the SendspinPlayer.
        """
        self._bridge_identifiers[client_id] = identifiers

    async def _handle_client_added(self, client_id: str, event_version: int) -> None:
        """Handle a new client connection asynchronously."""
        if self._unloading:
            return
        # Yield to allow any synchronous registration (like register_external_player) to complete
        # This is needed because ClientAddedEvent fires during get_or_create_client, before
        # preload_hello sets the client info
        await asyncio.sleep(0)
        # Check if client still exists (may have disconnected while waiting)
        sendspin_client = self.server_api.get_client(client_id)
        if sendspin_client is None:
            self.logger.debug("Client %s disconnected before hello completed", client_id)
            return
        # Wait for client hello to be processed (info becomes available)
        # ClientAddedEvent fires before the hello handshake completes
        for _ in range(50):  # Wait up to 5 seconds
            if sendspin_client._info is not None:
                break
            await asyncio.sleep(0.1)
        else:
            self.logger.warning("Client %s hello not received within timeout", client_id)
            return
        if not self._is_current_client_event(client_id, event_version):
            self.logger.debug("Skipping stale add event for %s", client_id)
            return
        if not self.mass.config.get_raw_player_config_value(client_id, CONF_ENABLED, True):
            self.logger.debug("Ignoring disabled sendspin client: %s", client_id)
            return
        extra_ids = self._bridge_identifiers.pop(client_id, None)
        existing_player = self.mass.players.get_player(client_id)
        if existing_player is not None:
            if not isinstance(existing_player, SendspinPlayer):
                self.logger.warning(
                    "Skipping Sendspin reconnect for %s: registered player has unexpected type %s",
                    client_id,
                    type(existing_player).__name__,
                )
                return
            await existing_player.reattach_client(sendspin_client, extra_ids)
            self.logger.debug("Client %s reconnected", client_id)
            if existing_player.device_info.manufacturer == "ESPHome" and (
                hass := self.mass.get_provider("hass")
            ):
                hass = cast("HomeAssistantProvider", hass)
                if hass_device := await hass.get_device_by_connection(client_id):
                    existing_player._attr_name = (
                        hass_device["name_by_user"] or hass_device["name"] or existing_player.name
                    )
            await self.mass.players.register_or_update(existing_player)
            return

        player = SendspinPlayer(self, client_id)
        # Apply any bridge identifiers that were pre-registered by the bridge manager.
        # This enables cross-protocol matching (e.g., Sendspin ↔ Chromecast via CAST_UUID).
        if extra_ids:
            for id_type, id_value in extra_ids.items():
                player.device_info.add_identifier(id_type, id_value)
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
        try:
            await self.mass.players.register(player)
        except AlreadyRegisteredError:
            self.logger.debug("Client %s already registered while handling add event", client_id)
            player._unsubscribe_client_callbacks()

    async def _handle_client_removed(self, client_id: str, event_version: int) -> None:
        """Handle a client disconnection asynchronously."""
        if self._unloading:
            return
        self.logger.debug("Client %s disconnected", client_id)
        if not self._is_current_client_event(client_id, event_version):
            self.logger.debug("Skipping stale remove event for %s", client_id)
            return
        if player := self.mass.players.get_player(client_id):
            if isinstance(player, SendspinPlayer):
                await player.mark_unavailable(
                    still_valid=lambda: self._is_current_client_event(client_id, event_version)
                )
                return
            self.logger.warning(
                "Skipping Sendspin disconnect handling for %s: registered player has unexpected "
                "type %s",
                client_id,
                type(player).__name__,
            )

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
        self._unloading = True
        player_ids = [player.player_id for player in self.players]
        # Disconnect all clients before stopping the server
        clients = list(self.server_api.clients)
        connected_clients = []
        disconnect_tasks = []
        for client in clients:
            if client.connection is None:
                continue
            connected_clients.append(client)
            disconnect_tasks.append(client.connection.disconnect(retry_connection=False))
        if disconnect_tasks:
            results = await asyncio.gather(*disconnect_tasks, return_exceptions=True)
            for client, result in zip(connected_clients, results, strict=True):
                if isinstance(result, Exception):
                    self.logger.warning(
                        "Error disconnecting client %s: %s", client.client_id, result
                    )

        # Stop the Sendspin server
        await self.server_api.close()

        for cb in self.unregister_cbs:
            cb()
        self.unregister_cbs = []
        await asyncio.gather(
            *(
                self.mass.players.unregister(player_id, permanent=is_removed)
                for player_id in player_ids
            ),
            return_exceptions=True,
        )
