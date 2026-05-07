"""
Sendspin Bridge for Chromecast - allows Sendspin to stream to Chromecast devices.

This module enables Chromecast devices to be controlled via the Sendspin protocol.
Unlike the AirPlay bridge, audio is NOT streamed through this bridge. Instead,
the bridge launches the Sendspin Cast Receiver app on the Chromecast, which has
a built-in JS Sendspin client that connects directly to the server via WebSocket.

The bridge:
1. Registers Chromecast players as external Sendspin clients (using MAC as client_id)
2. The Sendspin provider creates a SendspinPlayer for this external client
3. Protocol linking matches the SendspinPlayer with the ChromecastPlayer via MAC
4. When playback is requested, the Cast app is launched and connects to the server
5. The server upgrades the client from bridge role to the JS client's player@v1 role
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec
from aiosendspin.server import ClientRemovedEvent
from music_assistant_models.enums import EventType, IdentifierType
from pychromecast.controllers import BaseController

from music_assistant.helpers.util import format_ip_for_url, is_valid_mac_address
from music_assistant.providers.chromecast.constants import get_cast_model_static_delay
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.constants import (
    BRIDGE_PREFIX,
    CONF_SENDSPIN_STATIC_DELAY,
)
from music_assistant.providers.sendspin.helpers import (
    bridge_client_id_from_mac,
    bridge_client_id_from_uuid,
)

from .constants import SENDSPIN_CAST_APP_ID, SENDSPIN_CAST_BLOCKLIST, SENDSPIN_CAST_NAMESPACE

if TYPE_CHECKING:
    from aiosendspin.server import (
        ExternalStreamStartRequest,
        SendspinClient,
        SendspinEvent,
        SendspinServer,
    )
    from music_assistant_models.event import MassEvent
    from pychromecast.generated.cast_channel_pb2 import CastMessage

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .player import ChromecastPlayer
    from .provider import ChromecastProvider


_CAST_LOG_LEVEL_MAP: dict[str, int] = {
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


class SendspinCastController(BaseController):
    """Handles messages from the Sendspin Cast receiver app.

    Processes receiver_log messages (forwarded console output) and
    status messages (connection state, errors) from the Cast app.
    """

    def __init__(self, logger: logging.Logger) -> None:
        """Initialize the controller.

        :param logger: Logger to forward Cast receiver messages to.
        """
        super().__init__(SENDSPIN_CAST_NAMESPACE)
        self._log = logger

    def receive_message(self, _message: CastMessage, data: dict[str, Any]) -> bool:
        """Handle incoming messages on the Sendspin namespace.

        :param _message: The raw Cast protocol message.
        :param data: The parsed JSON payload.
        """
        msg_type = data.get("type")
        if msg_type == "receiver_log":
            return self._handle_receiver_log(data)
        if msg_type == "status":
            return self._handle_status(data)
        return False

    def _handle_receiver_log(self, data: dict[str, Any]) -> bool:
        """Forward a receiver console log to the Python logger."""
        level = _CAST_LOG_LEVEL_MAP.get(data.get("level", ""), logging.DEBUG)
        self._log.log(level, "[CastApp] %s", data.get("message", ""))
        if stack := data.get("stack"):
            self._log.log(level, "[CastApp] %s", stack)
        return True

    def _handle_status(self, data: dict[str, Any]) -> bool:
        """Handle a status message from the Cast receiver.

        Only errors are logged. Non-error statuses are sent every second and would be too noisy.
        """
        state = data.get("state")
        message = data.get("message", "")
        if state == "error":
            self._log.error("[CastApp] Error: %s", message)
        return True


def get_bridge_client_id(cast_player: ChromecastPlayer) -> str | None:
    """Get the Sendspin bridge client ID for a Chromecast player.

    Uses MAC address for physical devices, UUID for groups (which have no MAC).

    :param cast_player: The Chromecast player to bridge.
    :return: The bridge client_id, or None if no valid identifier is available.
    """
    if cast_player.cast_info.is_audio_group:
        return bridge_client_id_from_uuid(str(cast_player.cast_info.uuid))
    cast_mac = cast_player.cast_info.mac_address
    if cast_mac and is_valid_mac_address(cast_mac):
        return bridge_client_id_from_mac(cast_mac)
    device_mac = cast_player.device_info.mac_address
    if device_mac and is_valid_mac_address(device_mac):
        return bridge_client_id_from_mac(device_mac)
    return None


def _toggle_locally_administered_bit(mac_hex: str) -> str | None:
    """Toggle bit 1 (locally-administered bit) of the first octet of a hex MAC string.

    :param mac_hex: Lowercase 12-char hex MAC without separators (e.g. "5478c9e60da0").
    :return: The MAC with the LA bit toggled, or None if input is invalid.
    """
    if len(mac_hex) != 12:
        return None
    try:
        first_octet = int(mac_hex[:2], 16)
        toggled = first_octet ^ 0x02
        return f"{toggled:02x}{mac_hex[2:]}"
    except ValueError:
        return None


def is_sendspin_cast_blocked(manufacturer: str, model: str) -> bool:
    """Check if a device is blocked from the Sendspin Cast bridge.

    :param manufacturer: The device manufacturer name.
    :param model: The device model name.
    """
    for blocked_manufacturer, blocked_model in SENDSPIN_CAST_BLOCKLIST:
        if blocked_manufacturer in (manufacturer, "*") and blocked_model in (model, "*"):
            return True
    return False


def _build_bridge_hello(cast_player: ChromecastPlayer, bridge_client_id: str) -> ClientHelloPayload:
    """Build the Sendspin ClientHelloPayload used to register a Chromecast bridge."""
    return ClientHelloPayload(
        client_id=bridge_client_id,
        name=f"{cast_player.display_name} (Cast)",
        version=1,
        supported_roles=[BRIDGE_ROLE_ID],
        device_info=SendspinDeviceInfo(
            product_name="Chromecast Bridge",
            manufacturer=cast_player.device_info.manufacturer,
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


class SendspinChromecastBridge:
    """Manages the Sendspin to Chromecast bridge for a single player.

    This class handles:
    1. Registering the Chromecast player as an external Sendspin client
    2. Launching the Sendspin Cast Receiver app when playback is requested
    3. Sending the server URL and client_id to the Cast app via custom namespace

    The Cast app's built-in JS client then connects to the Sendspin server
    with the same client_id, and the server handles the reconnection/upgrade.
    """

    def __init__(
        self,
        provider: ChromecastProvider,
        cast_player: ChromecastPlayer,
        sendspin_server: SendspinServer,
        bridge_client_id: str,
    ) -> None:
        """Initialize the bridge.

        :param provider: The Chromecast provider instance.
        :param cast_player: The Chromecast player to bridge.
        :param sendspin_server: The Sendspin server to register with.
        :param bridge_client_id: The pre-resolved Sendspin client ID for this device.
        """
        self.provider = provider
        self.mass = provider.mass
        self.cast_player = cast_player
        self.sendspin_server = sendspin_server
        self.logger = provider.logger.getChild(f"bridge.{cast_player.player_id}")

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str = bridge_client_id
        self._bridge_role: BridgePlayerRole | None = None
        self._launch_task: asyncio.Task[None] | None = None
        self._log_controller: SendspinCastController | None = None

    @property
    def bridge_client_id(self) -> str:
        """Return the bridge client_id."""
        return self._bridge_client_id

    @property
    def is_cast_app_active(self) -> bool:
        """Return whether the Sendspin Cast app is active on the device."""
        return self.cast_player.cc.app_id == SENDSPIN_CAST_APP_ID

    async def start(self) -> None:
        """Register the Chromecast player as an external Sendspin client."""
        hello = _build_bridge_hello(self.cast_player, self._bridge_client_id)

        self.logger.debug(
            "Registering Sendspin bridge for %s with client_id=%s",
            self.cast_player.display_name,
            self._bridge_client_id,
        )

        self._sendspin_client = self.sendspin_server.register_external_player(
            hello, on_stream_start=self._on_stream_start
        )

        # Role is created by register_external_player via the factory registry.
        # Retrieve it and set up audio requirements so the server considers
        # this client ready for streaming (even though audio chunks are no-ops
        # since the JS client handles actual audio playback).
        roles = self._sendspin_client.roles_by_family("player")
        if roles:
            self._bridge_role = cast("BridgePlayerRole", roles[0])
            self._bridge_role.setup_audio_requirements()

        # Register log controller to receive receiver_log messages from the Cast app
        self._log_controller = SendspinCastController(self.logger)
        self.cast_player.cc.register_handler(self._log_controller)

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.cast_player.display_name,
            self._bridge_client_id,
        )

    async def stop(self) -> None:
        """Stop and unregister the Sendspin bridge."""
        if self._log_controller is not None:
            self.cast_player.cc.unregister_handler(self._log_controller)
            self._log_controller = None

        if self._launch_task and not self._launch_task.done():
            self._launch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._launch_task
            self._launch_task = None

        if self._sendspin_client:
            await self.sendspin_server.remove_client(self._bridge_client_id)
            self._sendspin_client = None
            self._bridge_role = None

        self.logger.debug("Sendspin bridge stopped for %s", self.cast_player.display_name)

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """Handle stream start request from Sendspin server.

        Called when Sendspin wants to play audio to this bridge player.
        Launches the Sendspin Cast Receiver app on the Chromecast device.
        The Cast app's JS client will connect to the server with the same
        client_id, taking over the connection from the bridge.
        """
        self.logger.debug(
            "Sendspin stream start request for %s (reason=%s)",
            self.cast_player.display_name,
            request.connection_reason,
        )
        if not self.cast_player.available:
            self.logger.warning("Cannot start Sendspin stream for %s: player not available")
            return
        # Cancel any previous launch task
        if self._launch_task and not self._launch_task.done():
            self._launch_task.cancel()
        self._launch_task = self.mass.create_task(self._launch_sendspin_app())

    async def _launch_sendspin_app(self) -> None:
        """Launch the Sendspin Cast Receiver app and send the server config."""
        try:
            # Launch the Sendspin Cast App on the Chromecast.
            # force_launch=True ensures the Cast device kills any running app
            # (including a stale Sendspin session) and starts a fresh instance,
            # so an explicit quit_app() beforehand is unnecessary.
            event = asyncio.Event()

            def launched_callback(
                success: bool,  # noqa: ARG001
                response: dict[str, Any] | None,  # noqa: ARG001
            ) -> None:
                self.mass.loop.call_soon_threadsafe(event.set)

            def launch() -> None:
                self.logger.debug(
                    "Launching Sendspin Cast App on %s", self.cast_player.display_name
                )
                self.cast_player.cc.socket_client.receiver_controller.launch_app(
                    SENDSPIN_CAST_APP_ID,
                    force_launch=True,
                    callback_function=launched_callback,
                )

            await self.mass.loop.run_in_executor(None, launch)
            await asyncio.wait_for(event.wait(), timeout=30.0)
            # Send config with retry — the Cast app's message listener
            # may not be ready immediately after the launch callback fires.
            await self._send_sendspin_config_with_retry()

            self.logger.info(
                "Sendspin Cast App launched on %s (client_id=%s)",
                self.cast_player.display_name,
                self._bridge_client_id,
            )
        except TimeoutError:
            self.logger.warning(
                "Timed out launching Sendspin Cast App on %s",
                self.cast_player.display_name,
            )
        except Exception as err:
            self.logger.error(
                "Failed to launch Sendspin Cast App on %s: %s",
                self.cast_player.display_name,
                err,
            )

    def _get_receiver_log_level(self) -> str:
        """Map the effective log level to a Cast receiver log level string."""
        effective = self.logger.getEffectiveLevel()
        if effective <= logging.DEBUG:
            return "debug"
        if effective <= logging.INFO:
            return "info"
        if effective <= logging.WARNING:
            return "warn"
        if effective <= logging.ERROR:
            return "error"
        return "off"

    async def _send_sendspin_config_with_retry(self, max_attempts: int = 3) -> None:
        """Send the Sendspin config to the Cast app, retrying on failure.

        The Cast app may not have its custom message listener registered
        immediately after launch. Retry with delays to handle this.

        :param max_attempts: Maximum number of send attempts.
        """
        for attempt in range(max_attempts):
            try:
                await self._send_sendspin_config()
                return
            except Exception as err:
                if attempt < max_attempts - 1:
                    self.logger.debug(
                        "Config send attempt %d/%d failed for %s: %s, retrying...",
                        attempt + 1,
                        max_attempts,
                        self.cast_player.display_name,
                        err,
                    )
                    await asyncio.sleep(2)
                else:
                    self.logger.warning(
                        "Failed to send config to Cast app on %s after %d attempts: %s",
                        self.cast_player.display_name,
                        max_attempts,
                        err,
                    )

    async def push_runtime_config_update(self) -> None:
        """Push updated runtime config to active Cast app."""
        await self._send_sendspin_config_with_retry()

    async def _send_sendspin_config(self) -> None:
        """Send the server URL, client_id, and settings to the Sendspin Cast app.

        The Cast app uses this info to connect its JS Sendspin client
        back to the server with the same client_id.
        """
        # The Sendspin server runs on its own port (8927), NOT through
        # the MA webserver or streams server. Use publish_ip directly.
        publish_ip = cast("str", self.mass.streams.publish_ip)
        server_url = f"ws://{format_ip_for_url(publish_ip)}:8927/sendspin"
        raw_delay = self.mass.config.get_raw_player_config_value(
            self._bridge_client_id, CONF_SENDSPIN_STATIC_DELAY
        )
        if raw_delay is None:
            sync_delay = get_cast_model_static_delay(
                self.cast_player.device_info.manufacturer or "",
                self.cast_player.device_info.model or "",
            )
        else:
            sync_delay = int(cast("int", raw_delay))
        # The Cast receiver JS reads playerId (not clientId) from the config.
        # It uses this as the client_id in its hello message to the Sendspin server,
        # allowing the server to match it to the bridge's pre-registered external client.
        message = {
            "type": "config",
            "serverUrl": server_url,
            "playerId": self._bridge_client_id,
            "playerName": f"{self.cast_player.display_name} (Cast)",
            "syncDelay": sync_delay,
            "codecs": ["flac"],
            "receiverLogLevel": self._get_receiver_log_level(),
        }

        def send() -> None:
            self.cast_player.cc.socket_client.send_app_message(SENDSPIN_CAST_NAMESPACE, message)

        await self.mass.loop.run_in_executor(None, send)
        self.logger.debug(
            "Sent Sendspin config to Cast app on %s: serverUrl=%s, playerId=%s, syncDelay=%dms",
            self.cast_player.display_name,
            message["serverUrl"],
            self._bridge_client_id,
            sync_delay,
        )


class SendspinBridgeManager:
    """Manages Sendspin bridges for all Chromecast players."""

    def __init__(self, provider: ChromecastProvider) -> None:
        """Initialize the bridge manager.

        :param provider: The Chromecast provider instance.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("bridge_manager")
        self._bridges: dict[str, SendspinChromecastBridge] = {}
        self._lock = asyncio.Lock()
        self._rebridge_unsubs: dict[str, Callable[[], None]] = {}
        self._claimed_clients: dict[str, str] = {}
        self._unsub_config_updated = self.mass.subscribe(
            self._on_player_config_updated, EventType.PLAYER_CONFIG_UPDATED
        )

    @property
    def sendspin_provider(self) -> SendspinProvider | None:
        """Get the Sendspin provider if available."""
        return cast(
            "SendspinProvider | None",
            self.mass.get_provider("sendspin"),
        )

    @property
    def sendspin_server(self) -> SendspinServer | None:
        """Get the Sendspin server if available."""
        if provider := self.sendspin_provider:
            return provider.server_api
        return None

    async def setup_bridge(self, cast_player: ChromecastPlayer) -> None:
        """
        Set up a Sendspin bridge for a Chromecast player.

        :param cast_player: The Chromecast player to bridge.
        """
        async with self._lock:
            player_id = cast_player.player_id

            if player_id in self._bridges:
                self.logger.debug("Bridge already exists for %s", cast_player.display_name)
                return

            if player_id in self._claimed_clients:
                self.logger.debug(
                    "Bridge already claimed for %s (waiting for JS client disconnect)",
                    cast_player.display_name,
                )
                return

            # Skip audio groups (non-stereo-pair) — they have their own playback mechanism
            if (
                cast_player.cast_info.is_audio_group
                and not cast_player.cast_info.is_multichannel_group
            ):
                return

            # skip if the cast player's parent also has airplay linked
            # (we prefer the airplay bridge due to better sync performance)
            if cast_player.protocol_parent_id:
                parent_player = self.mass.players.get_player(cast_player.protocol_parent_id)
                if parent_player:
                    for protocol in parent_player.linked_output_protocols:
                        if protocol.protocol_domain == "airplay":
                            return

            bridge_client_id = get_bridge_client_id(cast_player)
            if not bridge_client_id:
                return

            # Skip devices on the blocklist
            manufacturer = cast_player.device_info.manufacturer or ""
            model = cast_player.device_info.model or ""
            if is_sendspin_cast_blocked(manufacturer, model):
                self.logger.debug(
                    "Skipping Sendspin Cast bridge for %s (%s / %s) — device is blocklisted",
                    cast_player.display_name,
                    manufacturer,
                    model,
                )
                return

            sendspin_server = self.sendspin_server
            if not sendspin_server:
                self.logger.debug(
                    "Sendspin provider not available, skipping bridge for %s",
                    cast_player.display_name,
                )
                return

            existing_client_id = self._find_existing_sendspin_client(
                sendspin_server, bridge_client_id, cast_player
            )
            if existing_client_id:
                self.logger.info(
                    "Sendspin client %s already registered — claiming existing player for %s",
                    existing_client_id,
                    cast_player.display_name,
                )
                sendspin_provider = self.sendspin_provider
                if sendspin_provider is not None:
                    bridge_hello = _build_bridge_hello(cast_player, existing_client_id)
                    identifiers = {IdentifierType.CAST_UUID: str(cast_player.cast_info.uuid)}
                    sendspin_provider.register_bridge_static_delay_default(
                        existing_client_id, get_cast_model_static_delay(manufacturer, model)
                    )
                    if not await sendspin_provider.apply_bridge_claim(
                        existing_client_id, identifiers, bridge_hello
                    ):
                        # SendspinPlayer registration still in flight
                        # (`_handle_client_added` waits up to 5 s for hello).
                        # Pre-register identifiers so `_create_player` attaches
                        # CAST_UUID when it runs.
                        sendspin_provider.register_bridge_identifiers(
                            existing_client_id, identifiers
                        )
                    self._claimed_clients[player_id] = existing_client_id
                    self._subscribe_rebridge_on_disconnect(cast_player, existing_client_id)
                return

            bridge = SendspinChromecastBridge(
                self.provider, cast_player, sendspin_server, bridge_client_id
            )

            # Pre-register the Chromecast UUID so the resulting SendspinPlayer
            # carries it as a CAST_UUID identifier for cross-protocol matching.
            if sendspin_provider := self.sendspin_provider:
                sendspin_provider.register_bridge_identifiers(
                    bridge_client_id,
                    {IdentifierType.CAST_UUID: str(cast_player.cast_info.uuid)},
                )
                sendspin_provider.register_bridge_static_delay_default(
                    bridge_client_id, get_cast_model_static_delay(manufacturer, model)
                )

            try:
                await bridge.start()
            except Exception:
                self.logger.warning(
                    "Failed to start Sendspin bridge for %s", cast_player.display_name
                )
                with suppress(Exception):
                    await bridge.stop()
                return

            self._bridges[player_id] = bridge

            self.logger.info("Sendspin bridge created for %s", cast_player.display_name)

    async def remove_bridge(self, cast_player_id: str) -> None:
        """Remove the Sendspin bridge for a Chromecast player.

        :param cast_player_id: The player ID to remove the bridge for.
        """
        async with self._lock:
            if bridge := self._bridges.pop(cast_player_id, None):
                await bridge.stop()
            claimed_client_id = self._claimed_clients.pop(cast_player_id, None)
            if claimed_client_id and (unsub := self._rebridge_unsubs.pop(claimed_client_id, None)):
                with suppress(Exception):
                    unsub()

            self.logger.debug("Sendspin bridge removed for Chromecast player %s", cast_player_id)

    async def stop_all(self) -> None:
        """Stop all Sendspin bridges."""
        async with self._lock:
            for bridge in list(self._bridges.values()):
                with suppress(Exception):
                    await bridge.stop()
            self._bridges.clear()

        self.logger.debug("All Sendspin bridges stopped")

    async def close(self) -> None:
        """Stop all bridges and unsubscribe event listeners."""
        self._unsub_config_updated()
        for unsub in list(self._rebridge_unsubs.values()):
            with suppress(Exception):
                unsub()
        self._rebridge_unsubs.clear()
        self._claimed_clients.clear()
        await self.stop_all()

    def _find_existing_sendspin_client(
        self,
        sendspin_server: SendspinServer,
        bridge_client_id: str,
        cast_player: ChromecastPlayer,
    ) -> str | None:
        """
        Return a matching already-registered Sendspin client_id, if any.

        Happens on MA restart when the JS Cast receiver reconnects to the
        Sendspin server before Chromecast discovery fires. Also checks the
        LA-bit MAC variant (AirPlay uses locally-administered MAC, Chromecast
        uses the real one).
        """
        if sendspin_server.get_client(bridge_client_id):
            return bridge_client_id
        if cast_player.cast_info.is_audio_group:
            return None
        la_variant_mac = _toggle_locally_administered_bit(bridge_client_id[len(BRIDGE_PREFIX) :])
        if not la_variant_mac:
            return None
        la_variant_id = f"{BRIDGE_PREFIX}{la_variant_mac}"
        if sendspin_server.get_client(la_variant_id):
            return la_variant_id
        return None

    def _subscribe_rebridge_on_disconnect(
        self, cast_player: ChromecastPlayer, client_id: str
    ) -> None:
        """
        Subscribe a one-shot listener that re-runs setup_bridge on client disconnect.

        After claiming an already-registered external client (JS Cast receiver),
        the bridge's on_stream_start launch handler is not wired. When the JS
        client eventually disconnects (idle exit, device reboot), re-run
        setup_bridge so the fresh-client path registers the external player with
        a proper launch handler.
        """
        sendspin_provider = self.sendspin_provider
        if sendspin_provider is None:
            return
        if existing := self._rebridge_unsubs.pop(client_id, None):
            with suppress(Exception):
                existing()
        server_api = sendspin_provider.server_api

        def _listener(_server: SendspinServer, event: SendspinEvent) -> None:
            if not isinstance(event, ClientRemovedEvent):
                return
            if event.client_id != client_id:
                return
            unsub = self._rebridge_unsubs.pop(client_id, None)
            if unsub is not None:
                with suppress(Exception):
                    unsub()
            self._claimed_clients.pop(cast_player.player_id, None)
            if self.mass.players.get_player(cast_player.player_id) is not cast_player:
                return
            if cast_player.player_id in self._bridges:
                return
            self.mass.create_task(self.setup_bridge(cast_player))

        self._rebridge_unsubs[client_id] = server_api.add_event_listener(_listener)

    def get_bridge(self, cast_player_id: str) -> SendspinChromecastBridge | None:
        """Get the bridge for a Chromecast player.

        :param cast_player_id: The player ID to look up.
        """
        return self._bridges.get(cast_player_id)

    async def _on_player_config_updated(self, event: MassEvent) -> None:
        """Handle player config updates for bridged Sendspin Chromecast players."""
        if not event.object_id:
            return

        bridge: SendspinChromecastBridge | None = None
        async with self._lock:
            for candidate in self._bridges.values():
                if candidate.bridge_client_id == event.object_id:
                    bridge = candidate
                    break

        if not bridge:
            return

        if not bridge.is_cast_app_active:
            return

        self.mass.create_task(
            bridge.push_runtime_config_update,
            task_id=f"chromecast_sendspin_config_update_{bridge.cast_player.player_id}",
            abort_existing=True,
        )
