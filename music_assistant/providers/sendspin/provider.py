"""Player Provider for Sendspin."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from copy import deepcopy
from ipaddress import ip_address
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit
from uuid import uuid4

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from aiosendspin.server import (
    ClientAddedEvent,
    ClientRemovedEvent,
    ClientUpdatedEvent,
    SendspinEvent,
    SendspinServer,
)
from music_assistant_models.enums import (
    EventType,
    IdentifierType,
    PlayerFeature,
    PlayerType,
    ProviderFeature,
)
from music_assistant_models.errors import AlreadyRegisteredError, SetupFailedError

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    CONF_PLAYERS,
    CONF_PROVIDERS,
)
from music_assistant.helpers.util import format_ip_for_url
from music_assistant.mass import MusicAssistant
from music_assistant.models.player import Player
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.constants import (
    CONF_SENDSPIN_STATIC_DELAY,
    CONF_VIRTUAL_PLAYER_OWNER,
    VIRTUAL_PLAYER_ID_PREFIX,
)
from music_assistant.providers.sendspin.player import (
    SendspinBasePlayer,
    SendspinPlayer,
    SendspinVisualizerPlayer,
)

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient
    from aiosendspin.server.server import ExternalStreamStartRequest
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.providers.hass import HomeAssistantProvider


DEFAULT_SENDSPIN_CLIENT_PORT = 8928
DEFAULT_SENDSPIN_CLIENT_PATH = "/sendspin"
VIRTUAL_PLAYER_REGISTER_TIMEOUT = 10.0


def _manual_client_url(address: str) -> str:
    """Convert a manually configured Sendspin host/IP to a client WebSocket URL."""
    stripped_address = address.strip()
    if not stripped_address:
        raise ValueError("Address is empty")

    if "://" in stripped_address:
        return stripped_address

    try:
        parsed_ip = ip_address(stripped_address)
    except ValueError:
        pass
    else:
        return (
            f"ws://{format_ip_for_url(str(parsed_ip))}:"
            f"{DEFAULT_SENDSPIN_CLIENT_PORT}{DEFAULT_SENDSPIN_CLIENT_PATH}"
        )

    parsed_address = urlsplit(f"//{stripped_address}")
    if parsed_address.hostname is None:
        raise ValueError("Address does not contain a host")

    return (
        f"ws://{format_ip_for_url(parsed_address.hostname)}:"
        f"{parsed_address.port or DEFAULT_SENDSPIN_CLIENT_PORT}"
        f"{parsed_address.path or DEFAULT_SENDSPIN_CLIENT_PATH}"
    )


class SendspinProvider(PlayerProvider):
    """Player Provider for Sendspin."""

    server_api: SendspinServer
    unregister_cbs: list[Callable[[], None]]
    _pending_unregisters: dict[str, asyncio.Event]
    _bridge_identifiers: dict[str, dict[IdentifierType, str]]
    _bridge_underlying_players: dict[str, str]
    _bridge_static_delay_defaults: dict[str, int]
    _client_event_versions: dict[str, int]
    _client_event_task_counts: dict[str, int]
    _manual_ip_config: tuple[str, ...]
    _virtual_players: dict[str, str]
    _unloading: bool

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize a new Sendspin player provider."""
        super().__init__(mass, manifest, config)
        # Handle config option for manual IP's
        manual_ip_config = cast("list[str]", config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key))
        self._manual_ip_config = tuple(address for address in manual_ip_config if address.strip())
        self.server_api = SendspinServer(
            self.mass.loop, mass.server_id, "Music Assistant", self.mass.http_session
        )
        # Pitch (YINFFT) is the heaviest visualizer DSP and result quality is
        # still very mixed, needs more testing. Disable it globally for now to
        # spare low-power hosts.
        self.server_api.set_visualizer_pitch_enabled(enabled=False)
        self._pending_unregisters = {}
        self._bridge_identifiers = {}
        self._bridge_underlying_players = {}
        self._bridge_static_delay_defaults = {}
        self._bridge_player_types: dict[str, PlayerType] = {}
        self._client_event_versions = {}
        self._client_event_task_counts = {}
        self._virtual_players = {}
        self._unloading = False
        self.unregister_cbs = [
            self.server_api.add_event_listener(self.event_cb),
            self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED),
        ]

    def event_cb(self, server: SendspinServer, event: SendspinEvent) -> None:
        """Event callback registered to the sendspin server."""
        match event:
            case ClientAddedEvent(client_id):
                event_version = self._begin_client_event(client_id)
                self.mass.create_task(self._handle_client_added(client_id, event_version))
            case ClientRemovedEvent(client_id):
                event_version = self._begin_client_event(client_id)
                self.mass.create_task(self._handle_client_removed(client_id, event_version))
            case ClientUpdatedEvent(client_id):
                event_version = self._begin_client_event(client_id)
                self.mass.create_task(self._handle_client_updated(client_id, event_version))
            case _:
                self.logger.error("Unknown sendspin event: %s", event)

    def register_bridge_identifiers(
        self, client_id: str, identifiers: dict[IdentifierType, str]
    ) -> None:
        """
        Pre-register extra identifiers for a bridge client.

        Called by bridge managers (Chromecast, AirPlay) before registering an
        external player, so that the resulting SendspinPlayer carries the parent
        player's protocol-specific identifiers for cross-protocol matching.

        :param client_id: The bridge client_id that will be used for registration.
        :param identifiers: Extra identifiers to attach to the SendspinPlayer.
        """
        self._bridge_identifiers[client_id] = identifiers

    def register_bridge_underlying_player(self, client_id: str, underlying_player_id: str) -> None:
        """
        Pre-register the underlying player a bridge client runs on top of.

        Called by bridge managers before registering an external player, so that
        the resulting SendspinPlayer carries the derived-transport edge and the
        protocol linking layer can resolve its parent deterministically.

        :param client_id: The bridge client_id that will be used for registration.
        :param underlying_player_id: The player_id of the player the bridge rides on.
        """
        self._bridge_underlying_players[client_id] = underlying_player_id

    def register_bridge_static_delay_default(self, client_id: str, default_ms: int) -> None:
        """
        Register a protocol-specific default static delay for a bridge client.

        If the SendspinPlayer already exists, the default is applied immediately;
        otherwise it is stashed and picked up when the player is created.

        :param client_id: The bridge client_id for which the default applies.
        :param default_ms: Model-specific default static delay in milliseconds.
        """
        existing = self.mass.players.get_player(client_id)
        if isinstance(existing, SendspinPlayer):
            existing.static_delay_default_ms = default_ms
            # If no user-set value exists, push the new default to the device now
            # so already-connected clients pick it up without a config edit.
            if (
                self.mass.config.get_raw_player_config_value(client_id, CONF_SENDSPIN_STATIC_DELAY)
                is None
            ):
                self.mass.create_task(existing._apply_static_delay())
            return
        self._bridge_static_delay_defaults[client_id] = default_ms

    def register_bridge_player_type(self, client_id: str, player_type: PlayerType) -> None:
        """
        Pre-register a PlayerType override for a bridge client.

        Called by bridge managers to set the player type for the resulting
        player (e.g. PlayerType.LIGHT for Hue Entertainment bridges).
        """
        self._bridge_player_types[client_id] = player_type

    async def apply_bridge_claim(
        self,
        client_id: str,
        identifiers: dict[IdentifierType, str],
        bridge_hello: ClientHelloPayload,
        underlying_player_id: str | None = None,
    ) -> bool:
        """
        Post-claim an already-registered SendspinPlayer as a bridge client.

        Used when a bridge manager reaches setup_bridge AFTER the external client
        has already connected on its own (e.g. a JS Cast receiver reconnecting to
        the server before the Chromecast bridge could register). Attaches the
        bridge's protocol-specific identifiers so cross-protocol matching can
        link the SendspinPlayer to its native peer, and replays the bridge
        hello's supported_commands restriction on the player features.

        :param client_id: The Sendspin client_id whose player should be claimed.
        :param identifiers: Protocol-specific identifiers (e.g. CAST_UUID) to
            attach to the player for cross-protocol matching.
        :param bridge_hello: The bridge's intended ClientHelloPayload. Its
            player_support.supported_commands gates which volume/mute features
            the player is allowed to expose.
        :param underlying_player_id: The player_id of the player the bridge rides
            on, establishing the derived-transport edge for protocol linking.
        :return: True if a matching SendspinPlayer was found and updated.
        """
        player = self.mass.players.get_player(client_id)
        if not isinstance(player, SendspinPlayer):
            return False
        for id_type, id_value in identifiers.items():
            player.device_info.add_identifier(id_type, id_value)
        if underlying_player_id is not None:
            player._attr_underlying_player_id = underlying_player_id
        bridge_supported_commands: list[PlayerCommand] = []
        if bridge_hello.player_support:
            bridge_supported_commands = list(bridge_hello.player_support.supported_commands)
        if PlayerCommand.VOLUME in bridge_supported_commands:
            player._attr_supported_features.add(PlayerFeature.VOLUME_SET)
        else:
            player._attr_supported_features.discard(PlayerFeature.VOLUME_SET)
        if PlayerCommand.MUTE in bridge_supported_commands:
            player._attr_supported_features.add(PlayerFeature.VOLUME_MUTE)
        else:
            player._attr_supported_features.discard(PlayerFeature.VOLUME_MUTE)
        # Expose the claimed player as a protocol bridge, not a standalone web
        # player. A JS Cast receiver advertises product_name="Web Browser" and
        # would otherwise be classified as is_web_player → PlayerType.PLAYER
        # (hidden). Restore protocol semantics so UI links it under its native peer.
        player.is_web_player = False
        player._attr_hidden_by_default = False
        player._attr_expose_to_ha_by_default = True
        player._attr_type = PlayerType.PROTOCOL
        self.logger.info(
            "Bridge claim applied to existing SendspinPlayer %s (client_id=%s)",
            player.display_name,
            client_id,
        )
        await self.mass.players.register_or_update(player)
        return True

    async def create_virtual_player(
        self,
        owner_instance_id: str,
        display_name: str,
        player_id: str | None = None,
    ) -> str:
        """
        Create a hidden, server-side virtual Sendspin player.

        A virtual player owns its own PlayerQueue and leads a native Sendspin
        group, but never renders audio itself: the audio stream is delivered to
        the guest players that are attached to it through standard grouping.
        It is hidden from the UI and not exposed to Home Assistant by default,
        and is automatically removed when the owning provider unloads.

        :param owner_instance_id: Instance id of the (loaded) provider that owns
            the virtual player and controls its lifecycle.
        :param display_name: Human readable name for the virtual player.
        :param player_id: Optional stable id for the virtual player; a random id
            is generated when omitted. The id is always prefixed with
            ``VIRTUAL_PLAYER_ID_PREFIX``.
        :return: The player_id of the registered virtual player.
        :raises SetupFailedError: If the virtual player can not be created.
        """
        if (owner := self.mass.get_provider(owner_instance_id)) is None:
            raise SetupFailedError(f"Owner provider {owner_instance_id} is not loaded")
        if owner.instance_id != owner_instance_id and (
            len(self.mass.get_provider_instances(owner.domain)) > 1
        ):
            raise SetupFailedError(
                f"Multiple instances exist for {owner_instance_id}: pass an exact instance id"
            )
        # normalize a provider domain to the actual instance id
        owner_instance_id = owner.instance_id
        if player_id is None:
            player_id = uuid4().hex
        elif not re.fullmatch(r"[a-zA-Z0-9_-]+", player_id):
            raise SetupFailedError(
                f"Invalid player_id {player_id}: only alphanumerics, '_' and '-' are allowed"
            )
        if not player_id.startswith(VIRTUAL_PLAYER_ID_PREFIX):
            player_id = f"{VIRTUAL_PLAYER_ID_PREFIX}{player_id}"
        if player_id in self._virtual_players or self.server_api.get_client(player_id) is not None:
            raise SetupFailedError(f"Virtual player {player_id} already exists")
        # a persisted config may only be reclaimed by the same owner
        stored_owner = self._get_virtual_player_config_owner(player_id)
        if stored_owner is not None and stored_owner != owner_instance_id:
            raise SetupFailedError(f"Virtual player {player_id} is owned by {stored_owner}")
        self._virtual_players[player_id] = owner_instance_id
        try:
            self._register_virtual_player_client(player_id, display_name)
            await self._wait_for_virtual_player(player_id)
        except Exception as err:
            self._virtual_players.pop(player_id, None)
            # purge any partially registered player and its config
            await self.mass.players.unregister(player_id, permanent=True)
            if self.server_api.get_client(player_id) is not None:
                await self.server_api.remove_client(player_id)
            self.mass.players.delete_player_config(player_id)
            if isinstance(err, SetupFailedError):
                raise
            raise SetupFailedError(f"Failed to create virtual player {player_id}") from err
        # persist the owner so orphaned configs can be swept after a restart
        self.mass.config.set_raw_player_config_value(
            player_id, CONF_VIRTUAL_PLAYER_OWNER, owner_instance_id
        )
        self.logger.info("Virtual player %s created for %s", player_id, owner_instance_id)
        return player_id

    async def remove_virtual_player(self, player_id: str) -> None:
        """
        Remove a virtual Sendspin player and permanently delete its configuration.

        :param player_id: The player_id returned by create_virtual_player.
        :raises ValueError: If the given player_id is not a (known) virtual player.
        """
        if not player_id.startswith(VIRTUAL_PLAYER_ID_PREFIX) or (
            player_id not in self._virtual_players
            and self._get_virtual_player_config_owner(player_id) is None
        ):
            raise ValueError(f"{player_id} is not a virtual player")
        self._virtual_players.pop(player_id, None)
        # unregister the player first so the client removed event handler
        # can not race us with a non-permanent unregister
        await self.mass.players.unregister(player_id, permanent=True)
        if self.server_api.get_client(player_id) is not None:
            await self.server_api.remove_client(player_id)
        # the config may linger when the player was never registered
        self.mass.players.delete_player_config(player_id)
        self.logger.info("Virtual player %s removed", player_id)

    def is_virtual_player(self, player_id: str) -> bool:
        """Return whether the given player_id belongs to a registered virtual player."""
        return player_id in self._virtual_players

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.SYNC_PLAYERS,
        }

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        self._remove_orphan_virtual_player_configs()
        # Start server for handling incoming Sendspin connections from clients
        # and mDNS discovery of new clients
        await self.server_api.start_server(
            port=8927,
            host=self.mass.streams.bind_ip,
            advertise_addresses=[self.mass.streams.publish_ip],
        )
        for address in self._manual_ip_config:
            try:
                url = _manual_client_url(address)
            except ValueError as err:
                self.logger.warning(
                    "Ignoring invalid manual Sendspin client address %s: %s", address, err
                )
                continue
            self.logger.debug("Connecting to manually configured Sendspin client at %s", url)
            self.server_api.connect_to_client(
                url,
                retry_initial_connection=True,
                retry_indefinitely=True,
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
        self._client_event_task_counts.clear()
        self._client_event_versions.clear()
        self._virtual_players.clear()
        await asyncio.gather(
            *(
                self.mass.players.unregister(player_id, permanent=is_removed)
                for player_id in player_ids
            ),
            return_exceptions=True,
        )

    def _begin_client_event(self, client_id: str) -> int:
        """Increment version and in-flight task count for a client event."""
        version = self._client_event_versions.get(client_id, 0) + 1
        self._client_event_versions[client_id] = version
        self._client_event_task_counts[client_id] = (
            self._client_event_task_counts.get(client_id, 0) + 1
        )
        return version

    def _finish_client_event(self, client_id: str) -> None:
        """Drop in-flight bookkeeping and prune version state when idle."""
        task_count = self._client_event_task_counts.get(client_id, 0)
        if task_count <= 1:
            self._client_event_task_counts.pop(client_id, None)
            self._client_event_versions.pop(client_id, None)
            return
        self._client_event_task_counts[client_id] = task_count - 1

    def _is_current_client_event(self, client_id: str, event_version: int) -> bool:
        """Return True if the event version is still the latest for the client."""
        return self._client_event_versions.get(client_id) == event_version

    async def _apply_hass_name_override(self, player: SendspinBasePlayer, client_id: str) -> None:
        """Apply Home Assistant display name for ESPHome-backed Sendspin players."""
        if player.device_info.manufacturer != "ESPHome":
            return
        if not (hass := self.mass.get_provider("hass")):
            return
        hass = cast("HomeAssistantProvider", hass)
        if hass_device := await hass.get_device_by_connection(client_id):
            player._attr_name = hass_device["name_by_user"] or hass_device["name"] or player.name

    def _create_player(
        self,
        client_id: str,
        sendspin_client: SendspinClient,
        existing_player: Player | None,
        initial_hello: ClientHelloPayload | None = None,
    ) -> SendspinBasePlayer:
        """
        Create the appropriate player class based on client roles.

        Priority: player role -> SendspinPlayer, metadata role -> DISPLAY,
        visualizer role -> VISUALIZER. Bridge-registered type overrides the default.
        """
        extra_ids = self._bridge_identifiers.pop(client_id, None)
        bridge_player_type = self._bridge_player_types.pop(client_id, None)
        underlying_player_id = self._bridge_underlying_players.pop(client_id, None)
        if underlying_player_id is None and existing_player is not None:
            underlying_player_id = existing_player.underlying_player_id
        static_delay_default_ms = self._bridge_static_delay_defaults.pop(client_id, None)
        if static_delay_default_ms is None and isinstance(existing_player, SendspinPlayer):
            static_delay_default_ms = existing_player.static_delay_default_ms

        has_player_role = bool(sendspin_client.roles_by_family("player"))
        has_metadata_role = bool(sendspin_client.roles_by_family("metadata"))
        has_visualizer_role = bool(sendspin_client.roles_by_family("visualizer"))

        if has_player_role:
            audio_player = SendspinPlayer(self, client_id, initial_hello=initial_hello)
            if isinstance(existing_player, SendspinPlayer):
                audio_player.preserve_control_features_from(existing_player)
            player: SendspinBasePlayer = audio_player
        elif has_metadata_role or has_visualizer_role:
            default_type = PlayerType.DISPLAY if has_metadata_role else PlayerType.VISUALIZER
            viz_player = SendspinVisualizerPlayer(self, client_id, initial_hello=initial_hello)
            viz_player._attr_type = bridge_player_type or default_type
            player = viz_player
        else:
            audio_player = SendspinPlayer(self, client_id, initial_hello=initial_hello)
            if isinstance(existing_player, SendspinPlayer):
                audio_player.preserve_control_features_from(existing_player)
            player = audio_player

        if extra_ids:
            for id_type, id_value in extra_ids.items():
                player.device_info.add_identifier(id_type, id_value)
        if underlying_player_id is not None:
            player._attr_underlying_player_id = underlying_player_id
        if static_delay_default_ms is not None and isinstance(player, SendspinPlayer):
            player.static_delay_default_ms = static_delay_default_ms
        return player

    async def _handle_client_added(self, client_id: str, event_version: int) -> None:
        """Handle a new client connection asynchronously."""
        try:
            if self._unloading:
                return
            sendspin_client = self.server_api.get_client(client_id)
            if sendspin_client is None:
                self.logger.debug("Client %s disconnected before add handling started", client_id)
                return
            bridge_hello_snapshot = None
            if client_id in self._bridge_identifiers and sendspin_client._info is not None:
                # Snapshot the bridges hello before a reconnect can overwrite it
                bridge_hello_snapshot = deepcopy(sendspin_client._info)
            if pending_event := self._pending_unregisters.get(client_id):
                self.logger.debug(
                    "Waiting for pending unregister of %s before registering", client_id
                )
                await pending_event.wait()
                if not self._is_current_client_event(client_id, event_version):
                    self.logger.debug("Skipping stale add event for %s after waiting", client_id)
                    return
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
            existing_player = self.mass.players.get_player(client_id)
            preserved_identifiers = (
                dict(existing_player.device_info.identifiers) if existing_player is not None else {}
            )
            if existing_player is not None:
                self.logger.debug("Refreshing existing player object for %s", client_id)
                await self.mass.players.unregister(client_id)
                if not self._is_current_client_event(client_id, event_version):
                    self.logger.debug("Skipping stale add event for %s after unregister", client_id)
                    return
                sendspin_client = self.server_api.get_client(client_id)
                if sendspin_client is None:
                    self.logger.debug("Client %s disconnected after unregister", client_id)
                    return

            player = self._create_player(
                client_id, sendspin_client, existing_player, bridge_hello_snapshot
            )
            for id_type, id_value in preserved_identifiers.items():
                player.device_info.add_identifier(id_type, id_value)
            self.logger.debug("Client %s connected", client_id)
            await self._apply_hass_name_override(player, client_id)
            if not self._is_current_client_event(client_id, event_version):
                self.logger.debug("Skipping stale add event for %s after name override", client_id)
                player._unsubscribe_client_callbacks()
                return
            try:
                await self.mass.players.register(player)
            except AlreadyRegisteredError:
                self.logger.debug(
                    "Client %s already registered while handling add event", client_id
                )
                player._unsubscribe_client_callbacks()
        finally:
            self._finish_client_event(client_id)

    async def _handle_client_removed(self, client_id: str, event_version: int) -> None:
        """Handle a client disconnection asynchronously."""
        try:
            if self._unloading:
                return
            self.logger.debug("Client %s disconnected", client_id)
            if not self._is_current_client_event(client_id, event_version):
                self.logger.debug("Skipping stale remove event for %s", client_id)
                return
            unregister_event = asyncio.Event()
            self._pending_unregisters[client_id] = unregister_event
            try:
                await self.mass.players.unregister(client_id)
            finally:
                self._pending_unregisters.pop(client_id, None)
                unregister_event.set()
        finally:
            self._finish_client_event(client_id)

    async def _handle_client_updated(self, client_id: str, event_version: int) -> None:
        """Handle a client whose hello payload changed on reconnect."""
        try:
            if self._unloading:
                return
            if pending_event := self._pending_unregisters.get(client_id):
                self.logger.debug("Waiting for pending unregister of %s before updating", client_id)
                await pending_event.wait()
                if not self._is_current_client_event(client_id, event_version):
                    self.logger.debug("Skipping stale update event for %s after waiting", client_id)
                    return
            sendspin_client = self.server_api.get_client(client_id)
            if sendspin_client is None:
                return
            if not self._is_current_client_event(client_id, event_version):
                self.logger.debug("Skipping stale update event for %s", client_id)
                return
            existing_player = self.mass.players.get_player(client_id)
            if not isinstance(existing_player, SendspinBasePlayer):
                return
            previous_device_info = existing_player.device_info
            previous_type = existing_player.type
            existing_player._refresh_client_info(sendspin_client)
            if isinstance(existing_player, SendspinPlayer):
                existing_player.restore_bridge_identity(previous_device_info, previous_type)
            await self._apply_hass_name_override(existing_player, client_id)
            if not self._is_current_client_event(client_id, event_version):
                self.logger.debug("Skipping stale update event for %s after refresh", client_id)
                return
            if previous_type == PlayerType.PROTOCOL and existing_player.type != PlayerType.PROTOCOL:
                existing_player.set_protocol_parent_id(None)
                existing_player._attr_underlying_player_id = None
            await self.mass.players.register_or_update(existing_player)
        finally:
            self._finish_client_event(client_id)

    def _get_virtual_player_config_owner(self, player_id: str) -> str | None:
        """Return the owner instance id from a stored virtual player config, if any."""
        raw_conf = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
        if not isinstance(raw_conf, dict) or raw_conf.get("provider") != self.instance_id:
            return None
        values = raw_conf.get("values")
        if not isinstance(values, dict):
            return None
        return cast("str | None", values.get(CONF_VIRTUAL_PLAYER_OWNER))

    def _register_virtual_player_client(self, player_id: str, display_name: str) -> None:
        """Register the silent external Sendspin client backing a virtual player."""
        hello = ClientHelloPayload(
            client_id=player_id,
            name=display_name,
            version=1,
            supported_roles=[BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(
                product_name="Virtual Player",
                manufacturer="Music Assistant",
            ),
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM,
                        channels=BRIDGE_CHANNELS,
                        sample_rate=BRIDGE_SAMPLE_RATE,
                        bit_depth=BRIDGE_BIT_DEPTH,
                    )
                ],
                buffer_capacity=1_000,
                supported_commands=[],
            ),
        )
        sendspin_client = self.server_api.register_external_player(
            hello, on_stream_start=self._on_virtual_player_stream_start
        )
        for role in sendspin_client.roles_by_family("player"):
            if not isinstance(role, BridgePlayerRole):
                continue
            # audio delivered to the role is simply discarded: the virtual player
            # only anchors the group, the members receive the actual stream
            role.set_callbacks(
                on_audio_chunk=_virtual_player_noop,
                on_volume_change=_virtual_player_noop,
                on_mute_change=_virtual_player_noop,
                on_stream_start=_virtual_player_noop,
                on_stream_end=_virtual_player_noop,
            )
            role.setup_audio_requirements()
            role.set_timing(required_lead_time_ms=0, min_buffer_ms=0)
            break

    async def _wait_for_virtual_player(self, player_id: str) -> None:
        """Wait until the virtual player is registered in MA with its queue."""
        deadline = self.mass.loop.time() + VIRTUAL_PLAYER_REGISTER_TIMEOUT
        while self.mass.loop.time() < deadline:
            if (
                self.mass.players.get_player(player_id) is not None
                and self.mass.player_queues.get(player_id) is not None
            ):
                return
            await asyncio.sleep(0.1)
        raise SetupFailedError(f"Virtual player {player_id} was not registered in time")

    def _on_virtual_player_stream_start(self, _request: ExternalStreamStartRequest) -> None:
        """Accept stream start requests for virtual players (nothing to connect)."""

    async def _on_providers_updated(self, event: MassEvent) -> None:
        """Remove virtual players whose owning provider is no longer loaded."""
        # during (server) shutdown all providers unload; the startup sweep
        # takes care of configs whose owner is really gone
        if self._unloading or self.mass.closing:
            return
        for player_id, owner_instance_id in list(self._virtual_players.items()):
            if self.mass.get_provider(owner_instance_id) is not None:
                continue
            self.logger.debug(
                "Removing virtual player %s: owner %s unloaded", player_id, owner_instance_id
            )
            await self.remove_virtual_player(player_id)

    def _remove_orphan_virtual_player_configs(self) -> None:
        """Delete stored configs of virtual players whose owner provider is gone."""
        all_player_configs = self.mass.config.get(CONF_PLAYERS, {})
        for player_id, raw_conf in list(all_player_configs.items()):
            if not isinstance(raw_conf, dict) or raw_conf.get("provider") != self.instance_id:
                continue
            values = raw_conf.get("values")
            if not isinstance(values, dict):
                values = {}
            owner_instance_id = values.get(CONF_VIRTUAL_PLAYER_OWNER)
            if owner_instance_id is None:
                continue
            if self.mass.config.get(f"{CONF_PROVIDERS}/{owner_instance_id}") is not None:
                # owner still configured; it will recreate its virtual players
                continue
            self.logger.debug("Removing orphan virtual player config %s", player_id)
            self.mass.players.delete_player_config(player_id)


def _virtual_player_noop(*_args: object) -> None:
    """No-op callback for virtual players, which never render audio locally."""
