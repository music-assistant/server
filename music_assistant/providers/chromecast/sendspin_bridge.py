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

from music_assistant.helpers.util import is_valid_mac_address
from music_assistant.providers.sendspin.bridge_role import (
    BRIDGE_BIT_DEPTH,
    BRIDGE_CHANNELS,
    BRIDGE_ROLE_ID,
    BRIDGE_SAMPLE_RATE,
    BridgePlayerRole,
)
from music_assistant.providers.sendspin.helpers import bridge_client_id_from_mac

from .constants import (
    CONF_SENDSPIN_CODEC,
    CONF_SENDSPIN_SYNC_DELAY,
    DEFAULT_SENDSPIN_CODEC,
    DEFAULT_SENDSPIN_SYNC_DELAY,
    SENDSPIN_CAST_APP_ID,
    SENDSPIN_CAST_NAMESPACE,
)

if TYPE_CHECKING:
    from aiosendspin.server import ExternalStreamStartRequest, SendspinClient, SendspinServer

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
    ) -> None:
        """Initialize the bridge.

        :param provider: The Chromecast provider instance.
        :param cast_player: The Chromecast player to bridge.
        :param sendspin_server: The Sendspin server to register with.
        """
        self.provider = provider
        self.mass = provider.mass
        self.cast_player = cast_player
        self.sendspin_server = sendspin_server
        self.logger = provider.logger.getChild(f"bridge.{cast_player.player_id}")

        self._sendspin_client: SendspinClient | None = None
        self._bridge_client_id: str | None = None
        self._bridge_role: BridgePlayerRole | None = None
        self._launch_task: asyncio.Task[None] | None = None

    @property
    def is_registered(self) -> bool:
        """Return whether the bridge is registered with Sendspin."""
        return self._sendspin_client is not None

    async def start(self) -> None:
        """Register the Chromecast player as an external Sendspin client."""
        self._bridge_client_id = get_bridge_client_id(self.cast_player)
        if not self._bridge_client_id:
            self.logger.warning(
                "Cannot create Sendspin bridge for %s: no valid MAC address",
                self.cast_player.display_name,
            )
            return

        # Check if another bridge (e.g. AirPlay) already registered this client_id.
        # Devices that support both AirPlay and Chromecast share the same MAC,
        # so only the first bridge to register wins.
        if self.sendspin_server.get_client(self._bridge_client_id):
            self.logger.debug(
                "Sendspin client %s already registered (likely by another bridge), "
                "skipping Chromecast bridge for %s",
                self._bridge_client_id,
                self.cast_player.display_name,
            )
            self._bridge_client_id = None
            return

        hello = ClientHelloPayload(
            client_id=self._bridge_client_id,
            name=f"{self.cast_player.display_name} (Cast)",
            version=1,
            supported_roles=[BRIDGE_ROLE_ID],
            device_info=SendspinDeviceInfo(
                product_name=self.cast_player.device_info.model,
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

        if self._sendspin_client and self._bridge_client_id:
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
        # Cancel any previous launch task
        if self._launch_task and not self._launch_task.done():
            self._launch_task.cancel()
        self._launch_task = self.mass.create_task(self._launch_sendspin_app())

    async def _launch_sendspin_app(self) -> None:
        """Launch the Sendspin Cast Receiver app and send the server config."""
        if not self._bridge_client_id:
            return

        try:
            # Launch the Sendspin Cast App on the Chromecast
            event = asyncio.Event()

            def launched_callback(
                success: bool,  # noqa: ARG001
                response: dict[str, Any] | None,  # noqa: ARG001
            ) -> None:
                self.mass.loop.call_soon_threadsafe(event.set)

            def launch() -> None:
                cc = self.cast_player.cc
                if cc.app_id is not None:
                    cc.quit_app()
                self.logger.debug(
                    "Launching Sendspin Cast App on %s", self.cast_player.display_name
                )
                cc.socket_client.receiver_controller.launch_app(
                    SENDSPIN_CAST_APP_ID,
                    force_launch=True,
                    callback_function=launched_callback,
                )

            await self.mass.loop.run_in_executor(None, launch)
            await asyncio.wait_for(event.wait(), timeout=30.0)

            # Send the server URL and client_id to the Cast app
            await self._send_sendspin_config()

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
        """Get the sync delay from the player's config."""
        return int(
            self.mass.config.get_raw_player_config_value(
                self.cast_player.player_id,
                CONF_SENDSPIN_SYNC_DELAY,
                DEFAULT_SENDSPIN_SYNC_DELAY,
            )
        )

    def _get_codec(self) -> str:
        """Get the codec from the player's config."""
        return str(
            self.mass.config.get_raw_player_config_value(
                self.cast_player.player_id,
                CONF_SENDSPIN_CODEC,
                DEFAULT_SENDSPIN_CODEC,
            )
        )

    async def send_config_update(self) -> None:
        """Resend the Sendspin config to the Cast app.

        Called when the player's config changes (e.g. sync delay or codec updated).
        Only sends if the Cast app is currently running on the device.
        """
        if not self._bridge_client_id:
            return
        if self.cast_player.cc.app_id != SENDSPIN_CAST_APP_ID:
            return
        await self._send_sendspin_config()

    async def _send_sendspin_config(self) -> None:
        """Send the server URL, client_id, and settings to the Sendspin Cast app.

        The Cast app uses this info to connect its JS Sendspin client
        back to the server with the same client_id.
        """
        if not self._bridge_client_id:
            return

        server_url = self.mass.streams.base_url.replace("http", "ws")
        sync_delay = self._get_sync_delay()
        codec = self._get_codec()
        message = {
            "type": "CONFIG",
            "serverUrl": f"{server_url}/sendspin",
            "clientId": self._bridge_client_id,
            "syncDelay": sync_delay,
            "codecs": [codec],
        }

        def send() -> None:
            self.cast_player.cc.socket_client.send_app_message(SENDSPIN_CAST_NAMESPACE, message)

        await self.mass.loop.run_in_executor(None, send)
        self.logger.debug(
            "Sent Sendspin config to Cast app on %s: "
            "serverUrl=%s, clientId=%s, syncDelay=%dms, codecs=%s",
            self.cast_player.display_name,
            message["serverUrl"],
            self._bridge_client_id,
            sync_delay,
            [codec],
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
        """Set up a Sendspin bridge for a Chromecast player.

        Groups and stereo pairs are skipped (they have no MAC address).

        :param cast_player: The Chromecast player to bridge.
        """
        async with self._lock:
            player_id = cast_player.player_id

            sendspin_server = self.sendspin_server
            if not sendspin_server:
                self.logger.debug(
                    "Sendspin provider not available, skipping bridge for %s",
                    cast_player.display_name,
                )
                return

            if player_id in self._bridges:
                self.logger.debug("Bridge already exists for %s", cast_player.display_name)
                return

            bridge = SendspinChromecastBridge(self.provider, cast_player, sendspin_server)

            try:
                await bridge.start()
            except Exception:
                self.logger.warning(
                    "Failed to start Sendspin bridge for %s", cast_player.display_name
                )
                with suppress(Exception):
                    await bridge.stop()
                return

            if not bridge.is_registered:
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

    def get_bridge(self, cast_player_id: str) -> SendspinChromecastBridge | None:
        """Get the bridge for a Chromecast player.

        :param cast_player_id: The player ID to look up.
        """
        return self._bridges.get(cast_player_id)
