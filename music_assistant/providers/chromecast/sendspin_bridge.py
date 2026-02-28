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
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from aiosendspin.models.core import ClientHelloPayload
from aiosendspin.models.core import DeviceInfo as SendspinDeviceInfo
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand
from music_assistant_models.enums import EventType, IdentifierType

from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.constants import (
    CONF_SENDSPIN_SYNC_DELAY,
    DEFAULT_SENDSPIN_SYNC_DELAY,
)
from music_assistant.providers.sendspin.helpers import bridge_client_id_from_mac

from .constants import SENDSPIN_CAST_APP_ID, SENDSPIN_CAST_BLOCKLIST, SENDSPIN_CAST_NAMESPACE

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer
    from music_assistant_models.event import MassEvent

    from music_assistant.providers.sendspin.provider import SendspinProvider

    from .player import ChromecastPlayer
    from .provider import ChromecastProvider


def get_bridge_client_id(cast_player: ChromecastPlayer) -> str | None:
    """Get the Sendspin bridge client ID for a Chromecast player.

    Uses the MAC address as the client_id to enable protocol linking.
    The Sendspin provider will create a SendspinPlayer with this client_id.

    Checks cast_info.mac_address first (from eureka_info API), then falls
    back to the player's device_info MAC (which may have been resolved via
    ARP by the Players controller after registration).

    :param cast_player: The Chromecast player to bridge.
    :return: The bridge client_id, or None if no valid MAC address is available.
    """
    cast_mac = cast_player.cast_info.mac_address
    if cast_mac and is_valid_mac_address(cast_mac):
        return bridge_client_id_from_mac(cast_mac)
    device_mac = cast_player.device_info.mac_address
    if device_mac and is_valid_mac_address(device_mac):
        return bridge_client_id_from_mac(device_mac)
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
        hello = ClientHelloPayload(
            client_id=self._bridge_client_id,
            name=f"{self.cast_player.display_name} (Cast)",
            version=1,
            supported_roles=[BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(
                product_name="Chromecast Bridge",
                manufacturer=self.cast_player.device_info.manufacturer,
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
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
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

        self.logger.info(
            "Sendspin bridge registered for %s (client_id=%s)",
            self.cast_player.display_name,
            self._bridge_client_id,
        )

    async def stop(self) -> None:
        """Stop and unregister the Sendspin bridge."""
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

    def _get_sync_delay(self) -> int:
        """Get the sync delay from the Sendspin player's config."""
        return int(
            self.mass.config.get_raw_player_config_value(
                self._bridge_client_id,
                CONF_SENDSPIN_SYNC_DELAY,
                DEFAULT_SENDSPIN_SYNC_DELAY,
            )
        )

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
        """Push updated runtime config (including sync delay) to active Cast app."""
        await self._send_sendspin_config_with_retry()

    async def _send_sendspin_config(self) -> None:
        """Send the server URL, client_id, and settings to the Sendspin Cast app.

        The Cast app uses this info to connect its JS Sendspin client
        back to the server with the same client_id.
        """
        # The Sendspin server runs on its own port (8927), NOT through
        # the MA webserver or streams server. Use publish_ip directly.
        publish_ip = self.mass.streams.publish_ip
        server_url = f"ws://{publish_ip}:8927/sendspin"
        sync_delay = self._get_sync_delay()
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

            # Skip bridging for audio groups and multichannel children
            if cast_player.cast_info.is_audio_group:
                return
            if cast_player.cast_info.is_multichannel_child:
                return

            # skip if the cast player's parent also has airplay linked
            # (we prefer the airplay bridge due to better sync performance))
            if cast_player.protocol_parent_id:
                parent_player = self.mass.players.get_player(cast_player.protocol_parent_id)
                for protocol in parent_player.linked_output_protocols:
                    if protocol.protocol_domain == "airplay":
                        return

            # Resolve client_id from device MAC address
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

            # Check if another bridge (e.g. AirPlay) already registered this client_id.
            # Devices that support both AirPlay and Chromecast share the same MAC,
            # so only the first bridge to register wins.
            if sendspin_server.get_client(bridge_client_id):
                self.logger.debug(
                    "Sendspin client %s already registered (likely by another bridge), "
                    "skipping Chromecast bridge for %s",
                    bridge_client_id,
                    cast_player.display_name,
                )
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
        await self.stop_all()

    def get_bridge(self, cast_player_id: str) -> SendspinChromecastBridge | None:
        """Get the bridge for a Chromecast player.

        :param cast_player_id: The player ID to look up.
        """
        return self._bridges.get(cast_player_id)

    async def _on_player_config_updated(self, event: MassEvent) -> None:
        """Handle player config updates for bridged Sendspin Chromecast players."""
        # NOTE: This is a temporary solution for updating the sync delay until https://github.com/Sendspin/spec/pull/67
        # is implemented in aiosendspin, sendspin-js, and the cast app
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
