"""Remote Access subcomponent for the Webserver Controller.

This module manages WebRTC-based remote access to Music Assistant instances.
It connects to a signaling server and handles incoming WebRTC connections,
bridging them to the local WebSocket API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mashumaro import DataClassDictMixin
from music_assistant_models.enums import EventType

from music_assistant.constants import CONF_CORE
from music_assistant.controllers.webserver.remote_access.gateway import (
    WebRTCGateway,
    generate_remote_id,
)

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.controllers.webserver import WebserverController
    from music_assistant.providers.hass import HomeAssistantProvider

# Signaling server URL
SIGNALING_SERVER_URL = "wss://signaling.music-assistant.io/ws"

CONF_KEY_MAIN = "remote_access"
CONF_REMOTE_ID = "remote_id"
CONF_ENABLED = "enabled"


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
        self._remote_id: str | None = None
        self._enabled: bool = False
        self._using_ha_cloud: bool = False

    async def setup(self) -> None:
        """Initialize the remote access manager."""
        enabled_value = self.mass.config.get(f"{CONF_CORE}/{CONF_KEY_MAIN}/{CONF_ENABLED}", False)
        self._enabled = bool(enabled_value)
        remote_id_value = self.mass.config.get(
            f"{CONF_CORE}/{CONF_KEY_MAIN}/{CONF_REMOTE_ID}", None
        )
        if not remote_id_value:
            remote_id_value = generate_remote_id()
            self.mass.config.set(f"{CONF_CORE}/{CONF_KEY_MAIN}/{CONF_REMOTE_ID}", remote_id_value)

        self._remote_id = str(remote_id_value)
        self._register_api_commands()
        self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED)
        if self._enabled:
            await self.start()

    async def close(self) -> None:
        """Cleanup on exit."""
        await self.stop()

    async def start(self) -> None:
        """Start the remote access gateway."""
        if self.is_running or not self._enabled:
            return

        base_url = self.mass.webserver.base_url
        local_ws_url = base_url.replace("http", "ws")
        if not local_ws_url.endswith("/"):
            local_ws_url += "/"
        local_ws_url += "ws"

        ha_cloud_available, ice_servers = await self._get_ha_cloud_status()
        self._using_ha_cloud = bool(ha_cloud_available and ice_servers)

        mode = "optimized" if self._using_ha_cloud else "basic"
        self.logger.info("Starting remote access in %s mode (ID: %s)", mode, self._remote_id)

        self.gateway = WebRTCGateway(
            http_session=self.mass.http_session,
            signaling_url=SIGNALING_SERVER_URL,
            local_ws_url=local_ws_url,
            remote_id=self._remote_id,
            ice_servers=ice_servers,
        )

        await self.gateway.start()
        self._enabled = True

    async def stop(self) -> None:
        """Stop the remote access gateway."""
        if self.gateway:
            await self.gateway.stop()
            self.gateway = None

    async def _on_providers_updated(self, event: MassEvent) -> None:
        """Handle providers updated event to detect HA Cloud status changes.

        :param event: The providers updated event.
        """
        if not self.is_running or not self._enabled:
            return

        ha_cloud_available, ice_servers = await self._get_ha_cloud_status()
        new_using_ha_cloud = bool(ha_cloud_available and ice_servers)

        if new_using_ha_cloud != self._using_ha_cloud:
            self.logger.info("HA Cloud status changed, restarting remote access")
            await self.stop()
            self.mass.create_task(self.start())

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

            return True, None

        except Exception:
            self.logger.exception("Error getting HA Cloud status")
            return False, None

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
    def remote_id(self) -> str | None:
        """Return the current Remote ID."""
        return self._remote_id

    def _register_api_commands(self) -> None:
        """Register API commands for remote access."""

        async def get_remote_access_info() -> RemoteAccessInfo:
            """Get remote access information."""
            return RemoteAccessInfo(
                enabled=self.is_enabled,
                running=self.is_running,
                connected=self.is_connected,
                remote_id=self._remote_id or "",
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
                await self.start()
            elif not self._enabled and self.is_running:
                await self.stop()
            return await get_remote_access_info()

        self.mass.register_api_command("remote_access/info", get_remote_access_info)
        self.mass.register_api_command(
            "remote_access/configure", configure_remote_access, required_role="admin"
        )
