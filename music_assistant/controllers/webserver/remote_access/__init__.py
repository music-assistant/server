"""Remote Access subcomponent for the Webserver Controller.

This module manages WebRTC-based remote access to Music Assistant instances.
It connects to a signaling server and handles incoming WebRTC connections,
bridging them to the local WebSocket API.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from awesomeversion import AwesomeVersion
from mashumaro import DataClassDictMixin
from music_assistant_models.enums import EventType

from music_assistant.constants import CONF_CORE
from music_assistant.controllers.webserver.remote_access.gateway import WebRTCGateway
from music_assistant.helpers.webrtc_certificate import (
    get_or_create_webrtc_certificate,
    get_remote_id_from_certificate,
)

if TYPE_CHECKING:
    from aiortc.rtcdtlstransport import RTCCertificate
    from music_assistant_models.event import MassEvent

    from music_assistant.controllers.webserver import WebserverController
    from music_assistant.providers.hass import HomeAssistantProvider

# Signaling server URL
SIGNALING_SERVER_URL = "wss://signaling.music-assistant.io/ws"

CONF_KEY_MAIN = "remote_access"
CONF_ENABLED = "enabled"

TASK_ID_START_GATEWAY = "remote_access_start_gateway"
STARTUP_DELAY = 5


@dataclass
class RemoteAccessInfo(DataClassDictMixin):
    """Remote Access information dataclass."""

    enabled: bool
    running: bool
    connected: bool
    remote_id: str
    using_ha_cloud: bool
    signaling_url: str


class RemoteAccessManager:
    """Manages WebRTC-based remote access for the webserver."""

    def __init__(self, webserver: WebserverController) -> None:
        """Initialize the remote access manager."""
        self.webserver = webserver
        self.mass = webserver.mass
        self.logger = webserver.logger.getChild("remote_access")
        self.gateway: WebRTCGateway | None = None
        self._remote_id: str
        self._certificate: RTCCertificate
        self._enabled: bool = False
        self._using_ha_cloud: bool = False
        self._on_unload_callbacks: list[Callable[[], None]] = []

    async def setup(self) -> None:
        """Initialize the remote access manager."""
        self._certificate = get_or_create_webrtc_certificate(self.mass.storage_path)

        self._remote_id = get_remote_id_from_certificate(self._certificate)

        enabled_value = self.mass.config.get(f"{CONF_CORE}/{CONF_KEY_MAIN}/{CONF_ENABLED}", False)
        self._enabled = bool(enabled_value)
        self._register_api_commands()
        self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED)
        if self._enabled:
            await self._schedule_start()

    async def close(self) -> None:
        """Cleanup on exit."""
        self.mass.cancel_timer(TASK_ID_START_GATEWAY)
        await self.stop()
        for unload_cb in self._on_unload_callbacks:
            unload_cb()

    async def _schedule_start(self) -> None:
        """Schedule a debounced gateway start, cancelling any existing connection first."""
        # Cancel any pending timer
        self.mass.cancel_timer(TASK_ID_START_GATEWAY)
        # Stop any existing gateway
        await self.stop()
        # Schedule new start
        self.logger.debug("Scheduling remote access gateway start in %s seconds", STARTUP_DELAY)
        self.mass.call_later(
            STARTUP_DELAY,
            self._start_gateway,
            task_id=TASK_ID_START_GATEWAY,
        )

    async def _start_gateway(self) -> None:
        """Start the remote access gateway (internal implementation)."""
        if not self._enabled:
            self.logger.debug("Remote access disabled, skipping start")
            return

        base_url = self.mass.webserver.base_url
        local_ws_url = base_url.replace("http", "ws")
        if not local_ws_url.endswith("/"):
            local_ws_url += "/"
        local_ws_url += "ws"

        ha_cloud_available, ice_servers = await self._get_ha_cloud_status()
        self._using_ha_cloud = bool(ha_cloud_available and ice_servers)

        mode = "optimized" if self._using_ha_cloud else "basic"
        self.logger.info("Starting remote access in %s mode", mode)

        self.gateway = WebRTCGateway(
            http_session=self.mass.http_session,
            remote_id=self._remote_id,
            certificate=self._certificate,
            signaling_url=SIGNALING_SERVER_URL,
            local_ws_url=local_ws_url,
            ice_servers=ice_servers,
            # Pass callback to get fresh ICE servers for each client connection
            # This ensures TURN credentials are always valid
            ice_servers_callback=self.get_ice_servers if ha_cloud_available else None,
        )

        await self.gateway.start()

    async def stop(self) -> None:
        """Stop the remote access gateway."""
        if self.gateway:
            await self.gateway.stop()
            self.gateway = None

    async def _on_providers_updated(self, event: MassEvent) -> None:
        """Handle providers updated event to detect HA Cloud status changes.

        :param event: The providers updated event.
        """
        if not self._enabled:
            return

        # Check if HA Cloud status changed
        ha_cloud_available, ice_servers = await self._get_ha_cloud_status()
        new_using_ha_cloud = bool(ha_cloud_available and ice_servers)

        if new_using_ha_cloud != self._using_ha_cloud:
            self.logger.info("HA Cloud status changed, restarting remote access")
            await self._schedule_start()

    async def _get_ha_cloud_status(self) -> tuple[bool, list[dict[str, str]] | None]:
        """Get Home Assistant Cloud status and ICE servers.

        :return: Tuple of (ha_cloud_available, ice_servers).
        """
        ha_provider = cast("HomeAssistantProvider | None", self.mass.get_provider("hass"))
        if not ha_provider:
            return False, None
        try:
            hass_client = ha_provider.hass
            if not hass_client or not hass_client.connected:
                return False, None

            result = await hass_client.send_command("cloud/status")
            logged_in = result.get("logged_in", False)
            active_subscription = result.get("active_subscription", False)
            if not (logged_in and active_subscription):
                return False, None
            # HA Cloud is available, get ICE servers
            # The cloud/webrtc/ice_servers command was added in HA 2025.12.0b6
            if AwesomeVersion(hass_client.version) >= AwesomeVersion("2025.12.0b6"):
                if ice_servers := await hass_client.send_command("cloud/webrtc/ice_servers"):
                    return True, ice_servers
            else:
                self.logger.debug(
                    "HA version %s not supported for optimized WebRTC mode "
                    "(requires 2025.12.0b6 or later)",
                    hass_client.version,
                )
            self.logger.debug("HA Cloud available but no ICE servers returned")
        except Exception as err:
            self.logger.exception("Error getting HA Cloud status: %s", err)
        return False, None

    async def get_ice_servers(self) -> list[dict[str, str]]:
        """Get ICE servers for WebRTC connections.

        Returns HA Cloud TURN servers if available, otherwise returns public STUN servers.
        This method can be called regardless of whether remote access is enabled.

        :return: List of ICE server configurations.
        """
        # Default public STUN servers
        default_ice_servers: list[dict[str, str]] = [
            {"urls": "stun:stun.l.google.com:19302"},
            {"urls": "stun:stun.cloudflare.com:3478"},
            {"urls": "stun:stun.home-assistant.io:3478"},
        ]

        # Try to get HA Cloud ICE servers
        _, ice_servers = await self._get_ha_cloud_status()
        if ice_servers:
            return ice_servers

        return default_ice_servers

    @property
    def is_enabled(self) -> bool:
        """Return whether WebRTC remote access is enabled."""
        return self._enabled

    @property
    def is_running(self) -> bool:
        """Return whether the gateway is running."""
        return self.gateway is not None and self.gateway.is_running

    @property
    def is_connected(self) -> bool:
        """Return whether the gateway is connected to the signaling server."""
        return self.gateway is not None and self.gateway.is_connected

    @property
    def remote_id(self) -> str:
        """Return the current Remote ID."""
        return self._remote_id

    @property
    def certificate(self) -> RTCCertificate:
        """Return the persistent WebRTC DTLS certificate."""
        return self._certificate

    def _register_api_commands(self) -> None:
        """Register API commands for remote access."""

        async def get_remote_access_info() -> RemoteAccessInfo:
            """Get remote access information."""
            return RemoteAccessInfo(
                enabled=self.is_enabled,
                running=self.is_running,
                connected=self.is_connected,
                remote_id=self._remote_id,
                using_ha_cloud=self._using_ha_cloud,
                signaling_url=SIGNALING_SERVER_URL,
            )

        async def configure_remote_access(enabled: bool) -> RemoteAccessInfo:
            """Configure remote access settings.

            :param enabled: Enable or disable remote access.
            """
            self._enabled = enabled
            self.mass.config.set(f"{CONF_CORE}/{CONF_KEY_MAIN}/{CONF_ENABLED}", enabled)
            if self._enabled and not self.is_running:
                await self._start_gateway()
            elif not self._enabled and self.is_running:
                await self.stop()
            return await get_remote_access_info()

        self._on_unload_callbacks.append(
            self.mass.register_api_command(
                "remote_access/info", get_remote_access_info, required_role="admin"
            )
        )
        self._on_unload_callbacks.append(
            self.mass.register_api_command(
                "remote_access/configure", configure_remote_access, required_role="admin"
            )
        )
