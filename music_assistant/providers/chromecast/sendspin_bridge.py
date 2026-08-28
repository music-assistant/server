"""
Sendspin Bridge for Chromecast - allows Sendspin to stream to Chromecast devices.

This module enables Chromecast devices to be controlled via the Sendspin protocol.
Unlike the AirPlay bridge, audio is NOT streamed through this bridge. Instead,
the bridge launches the Sendspin Cast Receiver app on the Chromecast, which has
a built-in JS Sendspin client that connects directly to the server via WebSocket.

The bridge:
1. Registers Chromecast players as external Sendspin clients (using MAC as client_id)
2. The Sendspin provider creates a SendspinPlayer for this external client
3. Protocol linking parents the SendspinPlayer next to the ChromecastPlayer via the
   declared underlying player (derived-transport edge)
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
from aiosendspin.models.types import AudioCodec, GoodbyeReason
from aiosendspin.server import ClientRemovedEvent
from music_assistant_models.enums import EventType, IdentifierType
from music_assistant_models.errors import PlayerCommandFailed
from pychromecast.controllers import BaseController

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_PLAYERS,
    CONF_PROTOCOL_EXPERIMENTAL_NOTE,
    CONF_PROTOCOL_PARENT_ID,
    SENDSPIN_SERVER_PORT,
)
from music_assistant.helpers.util import format_ip_for_url, is_valid_mac_address
from music_assistant.providers.chromecast.constants import get_cast_model_static_delay
from music_assistant.providers.sendspin.bridge_manager import SendspinBridgeManagerBase
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

from .constants import (
    CONF_SENDSPIN_OPT_OUT_PENDING,
    CONF_SENDSPIN_UNSUPPORTED,
    SENDSPIN_CAST_APP_ID,
    SENDSPIN_CAST_BLOCKLIST,
    SENDSPIN_CAST_EXPERIMENTAL_NOTE,
    SENDSPIN_CAST_NAMESPACE,
    SENDSPIN_LINK_WAIT_INTERVAL,
    SENDSPIN_LINK_WAIT_TRIES,
)

if TYPE_CHECKING:
    from aiosendspin.server import (
        ExternalStreamStartRequest,
        SendspinClient,
        SendspinEvent,
        SendspinServer,
    )
    from music_assistant_models.event import MassEvent
    from pychromecast.generated.cast_channel_pb2 import CastMessage

    from music_assistant.models.player import Player
    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .player import ChromecastPlayer
    from .provider import ChromecastProvider


_CAST_LOG_LEVEL_MAP: dict[str, int] = {
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

_BRIDGE_POLICY_STATE_FIELDS = {
    "device_info.identifiers",
    "device_info.manufacturer",
    "device_info.model",
    "output_protocols",
    "type",
}


class SendspinCastController(BaseController):
    """
    Handles messages from the Sendspin Cast receiver app.

    Processes receiver_log messages (forwarded console output) and
    status messages (connection state, errors) from the Cast app.
    """

    def __init__(
        self,
        logger: logging.Logger,
        on_fatal_audio_error: Callable[[], None] | None = None,
        on_cast_connected: Callable[[], None] | None = None,
    ) -> None:
        """
        Initialize the controller.

        :param logger: Logger to forward Cast receiver messages to.
        :param on_fatal_audio_error: Callback when Cast app reports audio is unsupported.
        :param on_cast_connected: Callback when Cast app reports it connected to Sendspin.
        """
        super().__init__(SENDSPIN_CAST_NAMESPACE)
        self._log = logger
        self._on_fatal_audio_error = on_fatal_audio_error
        self._on_cast_connected = on_cast_connected

    def receive_message(self, _message: CastMessage, data: dict[str, Any]) -> bool:
        """
        Handle incoming messages on the Sendspin namespace.

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
        """
        Handle a status message from the Cast receiver.

        Only errors are logged. Non-error statuses are sent every second and would be too noisy.
        """
        state = data.get("state")
        message = data.get("message", "")
        if state == "error":
            self._log.error("[CastApp] Error: %s", message)
            if "Audio output is not supported" in message and self._on_fatal_audio_error:
                self._on_fatal_audio_error()
        elif state == "connected" and self._on_cast_connected:
            self._on_cast_connected()
        return True


def get_bridge_client_id(cast_player: ChromecastPlayer) -> str | None:
    """
    Get the Sendspin bridge client ID for a Chromecast player.

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
    """
    Toggle bit 1 (locally-administered bit) of the first octet of a hex MAC string.

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
    """
    Check if a device is blocked from the Sendspin Cast bridge.

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


async def _apply_fatal_disconnect(client: SendspinClient, logger: logging.Logger) -> None:
    """Tear down playback for a dead Cast client: solo → stop, grouped → ungroup."""
    try:
        if len(client.group.clients) > 1:
            await client.ungroup()
        else:
            await client.group.stop()
    except Exception:
        logger.exception("Failed to tear down dead Cast client %s", client.client_id)


class SendspinChromecastBridge:
    """
    Manages the Sendspin to Chromecast bridge for a single player.

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
        """
        Initialize the bridge.

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
        self._cast_app_was_active: bool = False
        self._cast_app_connected: bool = False
        self._cast_app_ready: asyncio.Future[None] | None = None

    def reset_cast_app_ready(self) -> asyncio.Future[None]:
        """
        Return a fresh cast-ready future, cancelling any prior pending one.

        If the Sendspin Cast app is already running (either previously
        connected this session, or running from before MA restart), return a
        future that's already resolved so callers don't wait 30s for a
        "connected" status that won't fire again.
        """
        prior = self._cast_app_ready
        if prior is not None and not prior.done():
            prior.cancel()
        fut: asyncio.Future[None] = self.mass.loop.create_future()
        already_running = self.cast_player.cc.app_id == SENDSPIN_CAST_APP_ID
        if self._cast_app_connected or already_running:
            fut.set_result(None)
            self._cast_app_ready = fut
            return fut
        self._cast_app_ready = fut
        return fut

    def ensure_cast_app_ready(self) -> asyncio.Future[None]:
        """
        Return the current future, creating one only if missing.

        Leaves a resolved future intact so a stream-start fired after a
        successful connect doesn't replace it with a pending one that
        nothing will resolve.
        """
        fut = self._cast_app_ready
        if fut is None:
            fut = self.mass.loop.create_future()
            self._cast_app_ready = fut
        return fut

    @property
    def bridge_client_id(self) -> str:
        """Return the bridge client_id."""
        return self._bridge_client_id

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    @property
    def is_cast_app_active(self) -> bool:
        """Return whether the Sendspin Cast app is active on the device."""
        return self.cast_player.cc.app_id == SENDSPIN_CAST_APP_ID

    async def start(self) -> None:
        """Register the Chromecast player as an external Sendspin client."""
        hello = _build_bridge_hello(self.cast_player, self._bridge_client_id)

        # Pre-register the Chromecast UUID so the resulting SendspinPlayer
        # carries it as a CAST_UUID identifier for cross-protocol matching.
        if sendspin_provider := cast("SendspinProvider | None", self.mass.get_provider("sendspin")):
            sendspin_provider.register_bridge_identifiers(
                self._bridge_client_id,
                {IdentifierType.CAST_UUID: str(self.cast_player.cast_info.uuid)},
            )
            sendspin_provider.register_bridge_underlying_player(
                self._bridge_client_id, self.cast_player.player_id
            )
            sendspin_provider.register_bridge_static_delay_default(
                self._bridge_client_id,
                get_cast_model_static_delay(
                    self.cast_player.device_info.manufacturer or "",
                    self.cast_player.device_info.model or "",
                ),
            )

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
        self._log_controller = SendspinCastController(
            self.logger,
            on_fatal_audio_error=self._on_cast_fatal_audio_error,
            on_cast_connected=self._on_cast_connected,
        )
        self.cast_player.cc.register_handler(self._log_controller)

        self._cast_app_was_active = self.cast_player.cc.app_id == SENDSPIN_CAST_APP_ID
        self.cast_player.on_app_status_changed = self.on_cast_status_changed

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.cast_player.display_name,
            self._bridge_client_id,
        )

    async def stop(self) -> None:
        """Stop and unregister the Sendspin bridge."""
        self.cast_player.on_app_status_changed = None
        self._cast_app_connected = False

        if self._cast_app_ready is not None and not self._cast_app_ready.done():
            self._cast_app_ready.cancel()
        self._cast_app_ready = None

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

    def on_cast_status_changed(self, app_id: str | None) -> None:
        """
        Handle Cast app id change / connection loss (called from socket thread).

        :param app_id: The current Cast app id, or None if the device disconnected.
        """
        if app_id == SENDSPIN_CAST_APP_ID:
            return
        if not self._cast_app_was_active:
            return
        self.mass.loop.call_soon_threadsafe(self._handle_cast_app_gone, app_id)

    async def push_runtime_config_update(self) -> None:
        """Push updated runtime config to active Cast app."""
        await self._send_sendspin_config_with_retry()

    def _on_cast_fatal_audio_error(self) -> None:
        """Handle fatal audio error from Cast receiver (called from socket thread)."""
        self.mass.loop.call_soon_threadsafe(self.mass.create_task, self._handle_fatal_audio_error())

    async def _handle_fatal_audio_error(self) -> None:
        """
        Process fatal audio error on the event loop.

        Switches the Sendspin output off for this device and records why, so it is not
        offered again until the user turns it back on.
        """
        self.logger.error(
            "Cast device %s does not support AudioContext — audio playback unavailable",
            self.cast_player.display_name,
        )
        # Recorded on the Cast player, not on the bridge: the re-evaluation below takes
        # the bridge and its config away for good, so this device is never offered again.
        self.mass.config.set_raw_player_config_value(
            self.cast_player.player_id, CONF_SENDSPIN_UNSUPPORTED, True
        )
        # Bubbles up to the frontend toast via play_media / set_members awaiting this
        # future. Resolved before the re-evaluation below, which tears this bridge down.
        self._resolve_cast_app_ready(
            PlayerCommandFailed(
                f"Sendspin isn't supported on {self.cast_player.display_name}. "
                "Use the standard Cast protocol instead."
            )
        )
        await self.provider.bridge_manager.evaluate_bridge(self.cast_player)

    def _on_cast_connected(self) -> None:
        """Handle Cast app "connected" status (called from socket thread)."""
        self.mass.loop.call_soon_threadsafe(self._mark_cast_app_connected)

    def _mark_cast_app_connected(self) -> None:
        """Mark the Cast app as connected and resolve any pending future."""
        self._cast_app_connected = True
        self._resolve_cast_app_ready(None)

    def _resolve_cast_app_ready(self, error: BaseException | None) -> None:
        """
        Resolve the cast-ready future if still pending.

        :param error: Exception to set on the future, or None for success.
        """
        fut = self._cast_app_ready
        if fut is None or fut.done():
            return
        if error is None:
            fut.set_result(None)
        else:
            fut.set_exception(error)
            fut.exception()

    def _handle_cast_app_gone(self, app_id: str | None) -> None:
        """Process Cast app disappearance on the event loop."""
        if not self._cast_app_was_active:
            return
        self._cast_app_was_active = False
        self._cast_app_connected = False
        self.logger.info(
            "Sendspin Cast app no longer active on %s (app_id=%s) — detaching client",
            self.cast_player.display_name,
            app_id,
        )
        client = self._sendspin_client
        if client is not None:
            if client.is_connected:
                client.detach_connection(GoodbyeReason.SHUTDOWN)
            self.mass.create_task(_apply_fatal_disconnect(client, self.logger))
        # Bubbles up to the frontend toast via play_media / set_members awaiting this future.
        self._resolve_cast_app_ready(
            PlayerCommandFailed(
                f"Cast app on {self.cast_player.display_name} stopped before reporting ready."
            )
        )

    def _on_stream_start(self, request: ExternalStreamStartRequest) -> None:
        """
        Handle stream start request from Sendspin server.

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
        self.ensure_cast_app_ready()
        if not self.cast_player.available:
            self.logger.warning(
                "Cannot start Sendspin stream for %s: player not available",
                self.cast_player.display_name,
            )
            return
        # Cancel any previous launch task
        if self._launch_task and not self._launch_task.done():
            self._launch_task.cancel()
        self._launch_task = self.mass.create_task(self._launch_sendspin_app())

    async def _launch_sendspin_app(self) -> None:
        """Launch the Sendspin Cast Receiver app and send the server config."""
        self.cast_player.cancel_pending_app_quit()
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
            self._cast_app_was_active = True

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
        """
        Send the Sendspin config to the Cast app, retrying on failure.

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

    async def _send_sendspin_config(self) -> None:
        """
        Send the server URL, client_id, and settings to the Sendspin Cast app.

        The Cast app uses this info to connect its JS Sendspin client
        back to the server with the same client_id.
        """
        # The Sendspin server runs on its own port, NOT through
        # the MA webserver or streams server. Use publish_ip directly.
        publish_ip = self.mass.streams.publish_ip
        # sendspin-js's SendspinCore appends `/sendspin` to baseUrl when constructing
        # the WebSocket URL. Send the bare server URL here so it ends up correct.
        server_url = f"ws://{format_ip_for_url(publish_ip)}:{SENDSPIN_SERVER_PORT}"
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


class SendspinBridgeManager(SendspinBridgeManagerBase[SendspinChromecastBridge]):
    """Manages Sendspin bridges for all Chromecast players."""

    def __init__(self, provider: ChromecastProvider) -> None:
        """
        Initialize the bridge manager.

        :param provider: The Chromecast provider instance.
        """
        super().__init__(provider)
        self._rebridge_unsubs: dict[str, Callable[[], None]] = {}
        self._claimed_clients: dict[str, str] = {}
        self._blocklist_log_state: dict[str, tuple[str, str]] = {}
        self._pending_bridge_evaluations: set[str] = set()
        self._unsubs.append(
            self.mass.players.subscribe_player_state_update(self._on_player_state_updated)
        )
        self._unsubs.append(
            self.mass.subscribe(
                self._on_player_unregistered,
                (EventType.PLAYER_UPDATED, EventType.PLAYER_REMOVED),
            )
        )

    async def evaluate_bridge(self, player: Player) -> None:
        """
        Reconcile the Sendspin bridge state for a Chromecast player.

        :param player: The player to evaluate.
        """
        client_id = self._bridge_client_id(player)
        if (
            client_id
            and not self.mass.config.get(f"{CONF_PLAYERS}/{client_id}")
            and self.mass.config.get(f"{CONF_PLAYERS}/{player.player_id}")
            and self._should_have_bridge(player)
        ):
            # This device was never offered the bridge, so its output has to end up off.
            # Recorded before the bridge registers and before anything can go wrong with
            # applying it, because registration is what creates the config this reads.
            self.mass.config.set_raw_player_config_value(
                player.player_id, CONF_SENDSPIN_OPT_OUT_PENDING, True
            )
        await super().evaluate_bridge(player)
        if not client_id or not self._has_bridge(player.player_id):
            return
        if self.mass.config.get_raw_player_config_value(client_id, CONF_PROTOCOL_EXPERIMENTAL_NOTE):
            # the note is written last, so its presence means this output is settled
            return
        never_offered = bool(
            self.mass.config.get_raw_player_config_value(
                player.player_id, CONF_SENDSPIN_OPT_OUT_PENDING
            )
        )
        self.mass.create_task(
            self._flag_bridge_experimental(player.player_id, client_id, disable=never_offered),
            task_id=f"sendspin_cast_experimental_{client_id}",
        )

    async def remove_bridge(self, player_id: str, permanent: bool = False) -> None:
        """
        Remove the Sendspin bridge (or claimed client) for a Chromecast player.

        :param player_id: The player ID to remove the bridge for.
        :param permanent: Also remove the bridge client player and its config.
        """
        claimed_client_id = self._claimed_clients.pop(player_id, None)
        if claimed_client_id:
            if unsub := self._rebridge_unsubs.pop(claimed_client_id, None):
                with suppress(Exception):
                    unsub()
            if sendspin_server := self.sendspin_server:
                with suppress(Exception):
                    await sendspin_server.remove_client(claimed_client_id)
            if permanent:
                if self.mass.players.get_player(claimed_client_id):
                    await self.mass.players.unregister(claimed_client_id, permanent=True)
                else:
                    self.mass.players.delete_player_config(claimed_client_id)
            self.logger.debug("Claimed Sendspin client removed for Chromecast player %s", player_id)
        await super().remove_bridge(player_id, permanent=permanent)

    async def close(self) -> None:
        """Stop all bridges and unsubscribe event listeners."""
        for unsub in list(self._rebridge_unsubs.values()):
            with suppress(Exception):
                unsub()
        self._rebridge_unsubs.clear()
        self._claimed_clients.clear()
        self._blocklist_log_state.clear()
        self._pending_bridge_evaluations.clear()
        await super().close()

    def get_bridge_by_client_id(self, bridge_client_id: str) -> SendspinChromecastBridge | None:
        """
        Return the bridge whose `bridge_client_id` matches, if any.

        :param bridge_client_id: The Sendspin client_id used by a bridged Cast device.
        """
        for bridge in self._bridges.values():
            if bridge.bridge_client_id == bridge_client_id:
                return bridge
        return None

    def _bridge_client_id(self, player: Player) -> str | None:
        """Return the Sendspin client_id used to bridge a Chromecast player."""
        return get_bridge_client_id(cast("ChromecastPlayer", player))

    def _create_bridge(self, player: Player) -> SendspinChromecastBridge:
        """Create a bridge instance for a Chromecast player."""
        cast_player = cast("ChromecastPlayer", player)
        bridge_client_id = get_bridge_client_id(cast_player)
        sendspin_server = self.sendspin_server
        # both guaranteed by _should_have_bridge/_lifecycle_allows_bridge
        assert bridge_client_id is not None
        assert sendspin_server is not None
        return SendspinChromecastBridge(
            cast("ChromecastProvider", self.provider),
            cast_player,
            sendspin_server,
            bridge_client_id,
        )

    def _has_bridge(self, player_id: str) -> bool:
        """Return whether a bridge or claimed client is in place for the player."""
        return player_id in self._bridges or player_id in self._claimed_clients

    def _should_have_bridge(self, player: Player) -> bool:
        """
        Return whether this cast player should have a Sendspin bridge.

        :param player: The Chromecast player to evaluate.
        """
        cast_player = cast("ChromecastPlayer", player)
        # Audio groups (non-stereo-pair) have their own playback mechanism
        if cast_player.cast_info.is_audio_group and not cast_player.cast_info.is_multichannel_group:
            return False

        if not get_bridge_client_id(cast_player):
            return False

        if self.mass.config.get_raw_player_config_value(
            cast_player.player_id, CONF_SENDSPIN_UNSUPPORTED
        ):
            # the receiver told us it cannot run the Sendspin client, so do not offer it
            return False

        manufacturer = cast_player.device_info.manufacturer or ""
        model = cast_player.device_info.model or ""
        if is_sendspin_cast_blocked(manufacturer, model):
            blocklist_state = (manufacturer, model)
            if self._blocklist_log_state.get(cast_player.player_id) != blocklist_state:
                self.logger.debug(
                    "Skipping Sendspin Cast bridge for %s (%s / %s) — device is blocklisted",
                    cast_player.display_name,
                    manufacturer,
                    model,
                )
                self._blocklist_log_state[cast_player.player_id] = blocklist_state
            return False
        self._blocklist_log_state.pop(cast_player.player_id, None)

        # Prefer the airplay bridge over the cast bridge: the Cast receiver
        # pipeline is fragile on many (especially 3rd-party) devices. Hard-deny
        # the cast bridge whenever the device is known to also support AirPlay,
        # regardless of whether the airplay protocol/provider is currently
        # enabled - a device with AirPlay gets its Sendspin support over
        # AirPlay or not at all.
        if cast_player.protocol_parent_id:
            parent_player = self.mass.players.get_player(cast_player.protocol_parent_id)
            if parent_player and parent_player.get_output_protocol_by_domain("airplay"):
                return False

        return True

    async def _try_claim_existing(self, player: Player) -> bool:
        """
        Claim a Sendspin client that connected on its own (JS Cast receiver).

        Happens on MA restart when the JS Cast receiver reconnects to the
        Sendspin server before Chromecast discovery fires.

        :param player: The Chromecast player being bridged.
        :return: True when an existing client was claimed (skips bridge setup).
        """
        cast_player = cast("ChromecastPlayer", player)
        bridge_client_id = get_bridge_client_id(cast_player)
        sendspin_server = self.sendspin_server
        sendspin_provider = self.sendspin_provider
        if bridge_client_id is None or sendspin_server is None or sendspin_provider is None:
            return False
        existing_client_id = self._find_existing_sendspin_client(
            sendspin_server, bridge_client_id, cast_player
        )
        if not existing_client_id:
            return False

        self.logger.info(
            "Sendspin client %s already registered — claiming existing player for %s",
            existing_client_id,
            cast_player.display_name,
        )
        manufacturer = cast_player.device_info.manufacturer or ""
        model = cast_player.device_info.model or ""
        bridge_hello = _build_bridge_hello(cast_player, existing_client_id)
        identifiers = {IdentifierType.CAST_UUID: str(cast_player.cast_info.uuid)}
        sendspin_provider.register_bridge_static_delay_default(
            existing_client_id, get_cast_model_static_delay(manufacturer, model)
        )
        if not await sendspin_provider.apply_bridge_claim(
            existing_client_id,
            identifiers,
            bridge_hello,
            underlying_player_id=cast_player.player_id,
        ):
            # SendspinPlayer registration still in flight
            # (`_handle_client_added` waits up to 5 s for hello).
            # Pre-register identifiers so `_create_player` attaches
            # CAST_UUID when it runs.
            sendspin_provider.register_bridge_identifiers(existing_client_id, identifiers)
            sendspin_provider.register_bridge_underlying_player(
                existing_client_id, cast_player.player_id
            )
        self._claimed_clients[cast_player.player_id] = existing_client_id
        self._subscribe_rebridge_on_disconnect(cast_player, existing_client_id)
        return True

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

        event_unsub = server_api.add_event_listener(_listener)

        cast_app_was_active = cast_player.cc.app_id == SENDSPIN_CAST_APP_ID

        def _handle_app_gone(app_id: str | None) -> None:
            nonlocal cast_app_was_active
            if not cast_app_was_active:
                return
            cast_app_was_active = False
            self.logger.info(
                "Claimed Sendspin Cast client lost on %s (app_id=%s) — detaching",
                cast_player.display_name,
                app_id,
            )
            client = server_api.get_client(client_id)
            if client is not None:
                if client.is_connected:
                    client.detach_connection(GoodbyeReason.SHUTDOWN)
                self.mass.create_task(_apply_fatal_disconnect(client, self.logger))
            if cast_player.on_app_status_changed is _on_cast_status_changed:
                cast_player.on_app_status_changed = None

        def _on_cast_status_changed(app_id: str | None) -> None:
            if app_id == SENDSPIN_CAST_APP_ID:
                return
            if not cast_app_was_active:
                return
            self.mass.loop.call_soon_threadsafe(_handle_app_gone, app_id)

        cast_player.on_app_status_changed = _on_cast_status_changed

        def _combined_unsub() -> None:
            with suppress(Exception):
                event_unsub()
            if cast_player.on_app_status_changed is _on_cast_status_changed:
                cast_player.on_app_status_changed = None

        self._rebridge_unsubs[client_id] = _combined_unsub

    def _on_player_state_updated(
        self, updated_player: Player, changed_values: dict[str, tuple[Any, Any]]
    ) -> None:
        """Re-evaluate bridge state after a relevant player state change."""
        if not changed_values.keys() & _BRIDGE_POLICY_STATE_FIELDS:
            return
        match_ids = {updated_player.player_id}
        if updated_player.protocol_parent_id:
            match_ids.add(updated_player.protocol_parent_id)
        self._schedule_bridge_evaluations(match_ids)

    def _on_player_unregistered(self, event: MassEvent) -> None:
        """Re-evaluate Cast bridges after a possible parent is unregistered."""
        if not event.object_id or self.mass.players.get_player(event.object_id):
            return
        self._schedule_bridge_evaluations({event.object_id})

    def _schedule_bridge_evaluations(self, match_ids: set[str]) -> None:
        """Schedule reconciliation for Cast bridges matching a player ID."""
        for player in self.provider.players:
            cast_player = cast("ChromecastPlayer", player)
            if cast_player.player_id in match_ids or (
                cast_player.protocol_parent_id and cast_player.protocol_parent_id in match_ids
            ):
                self._pending_bridge_evaluations.add(cast_player.player_id)
                self.mass.create_task(
                    self._process_pending_bridge_evaluations,
                    cast_player.player_id,
                    task_id=f"evaluate_chromecast_sendspin_bridge_{cast_player.player_id}",
                )

    async def _process_pending_bridge_evaluations(self, player_id: str) -> None:
        """
        Reconcile all bridge policy updates queued for a Chromecast player.

        :param player_id: The Chromecast player ID to reconcile.
        """
        while player_id in self._pending_bridge_evaluations:
            self._pending_bridge_evaluations.discard(player_id)
            player = self.mass.players.get_player(player_id)
            if player is None or player.provider is not self.provider:
                return
            await self.evaluate_bridge(player)

    async def _on_player_config_updated(self, event: MassEvent) -> None:
        """Re-evaluate bridges and push runtime config updates to active Cast apps."""
        await super()._on_player_config_updated(event)
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

    async def _flag_bridge_experimental(
        self, player_id: str, client_id: str, disable: bool
    ) -> None:
        """
        Mark a bridged Cast device's Sendspin output as experimental, and opt it out.

        :param player_id: The Chromecast player the bridge belongs to.
        :param client_id: The Sendspin client_id of the bridge.
        :param disable: Switch the output off, leaving it for the user to opt in.
        """
        # The output's toggle and warning are built from the persisted protocol link, so
        # a bridge that is about to be switched off again has to be linked first or it
        # leaves nothing behind to opt in with.
        if not await self._wait_for_protocol_link(client_id):
            return
        if not self._has_bridge(player_id):
            # the bridge went away while we waited - its config may be gone with it
            return
        if disable:
            self.logger.info(
                "Sendspin over Cast is experimental: leaving it switched off for %s "
                "until it is enabled in the player settings",
                client_id,
            )
            await self.mass.config.save_player_config(client_id, {CONF_ENABLED: False})
        # written last: a missing note is what makes the next evaluation retry all of this
        self.mass.config.set_raw_player_config_value(
            client_id, CONF_PROTOCOL_EXPERIMENTAL_NOTE, SENDSPIN_CAST_EXPERIMENTAL_NOTE
        )
        self.mass.config.set_raw_player_config_value(player_id, CONF_SENDSPIN_OPT_OUT_PENDING, None)

    async def _wait_for_protocol_link(self, client_id: str) -> bool:
        """
        Wait for a bridge client's protocol link to be persisted.

        :param client_id: The Sendspin client_id of the bridge.
        :return: True once the link is stored, False when it did not appear in time.
        """
        for _ in range(SENDSPIN_LINK_WAIT_TRIES):
            parent_id = self.mass.config.get_raw_player_config_value(
                client_id, CONF_PROTOCOL_PARENT_ID
            )
            if parent_id and self.mass.config.get(f"{CONF_PLAYERS}/{parent_id}"):
                return True
            await asyncio.sleep(SENDSPIN_LINK_WAIT_INTERVAL)
        self.logger.warning(
            "Sendspin bridge %s was not linked to a parent player in time; "
            "its output is left as it is and retried on the next evaluation",
            client_id,
        )
        return False
