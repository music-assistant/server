"""
Remote Access subcomponent for the Webserver Controller.

This module manages WebRTC-based remote access to Music Assistant instances.
It connects to a signaling server and handles incoming WebRTC connections,
bridging them to the local WebSocket API.

Requires an active Home Assistant Cloud subscription due to STUN/TURN/SIGNALING server usage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mashumaro import DataClassDictMixin
from music_assistant_models.enums import EventType
from music_assistant_models.errors import UnsupportedFeaturedException

from music_assistant.constants import CONF_CORE
from music_assistant.controllers.webserver.remote_access.gateway import (
    WebRTCGateway,
    generate_remote_id,
)
from music_assistant.helpers.api import api_command

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.controllers.webserver import WebserverController
    from music_assistant.providers.hass import HomeAssistantProvider

# Signaling server URL
SIGNALING_SERVER_URL = "wss://signaling.music-assistant.io/ws"

# Storage keys
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
    ha_cloud_available: bool
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
        self._ha_cloud_available: bool = False

    async def setup(self) -> None:
        """Initialize the remote access manager."""
        # Load config from storage
        enabled_value = self.mass.config.get(f"{CONF_CORE}/{CONF_KEY_MAIN}/{CONF_ENABLED}", False)
        self._enabled = bool(enabled_value)
        remote_id_value = self.mass.config.get(
            f"{CONF_CORE}/{CONF_KEY_MAIN}/{CONF_REMOTE_ID}", None
        )
        if not remote_id_value:
            remote_id_value = generate_remote_id()
            self.mass.config.set(f"{CONF_CORE}/{CONF_KEY_MAIN}/{CONF_REMOTE_ID}", remote_id_value)
            self.logger.debug("Generated new Remote ID: %s", remote_id_value)

        self._remote_id = str(remote_id_value)
        self._register_api_commands()
        # Subscribe to provider updates to check for Home Assistant Cloud
        self._ha_cloud_available = await self._check_ha_cloud_status()
        self.mass.subscribe(
            self._on_providers_updated, EventType.PROVIDERS_UPDATED, id_filter="hass"
        )
        if self._enabled and self._ha_cloud_available:
            await self.start()

    async def close(self) -> None:
        """Cleanup on exit."""
        await self.stop()

    async def start(self) -> None:
        """Start the remote access gateway."""
        if self.is_running:
            self.logger.debug("Remote access already running")
            return
        if not self._ha_cloud_available:
            raise UnsupportedFeaturedException(
                "Home Assistant Cloud subscription is required for remote access"
            )
        if not self._enabled:
            # should not happen, but guard anyway
            self.logger.debug("Remote access is disabled in configuration")
            return

        self.logger.info("Starting remote access with Remote ID: %s", self._remote_id)

        # Determine local WebSocket URL from webserver config
        base_url = self.mass.webserver.base_url
        local_ws_url = base_url.replace("http", "ws")
        if not local_ws_url.endswith("/"):
            local_ws_url += "/"
        local_ws_url += "ws"

        # Get ICE servers from HA Cloud if available
        ice_servers: list[dict[str, str]] | None = None
        if await self._check_ha_cloud_status():
            self.logger.info(
                "Home Assistant Cloud subscription detected, using HA cloud ICE servers"
            )
            ice_servers = await self._get_ha_cloud_ice_servers()
        else:
            self.logger.info(
                "Home Assistant Cloud subscription not detected, using default STUN servers"
            )

        # Initialize and start the WebRTC gateway
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
            self.logger.debug("WebRTC Remote Access stopped")

    async def _on_providers_updated(self, event: MassEvent) -> None:
        """
        Handle providers updated event.

        :param event: The providers updated event.
        """
        last_ha_cloud_available = self._ha_cloud_available
        self._ha_cloud_available = await self._check_ha_cloud_status()
        if self._ha_cloud_available == last_ha_cloud_available:
            return  # No change in HA Cloud status
        if self.is_running and not self._ha_cloud_available:
            self.logger.warning(
                "Home Assistant Cloud subscription is no longer active, stopping remote access"
            )
            await self.stop()
            return
        allow_start = self._ha_cloud_available and self._enabled
        if allow_start and self.is_running:
            return  # Already running
        if allow_start:
            self.mass.create_task(self.start())

    async def _check_ha_cloud_status(self) -> bool:
        """Check if Home Assistant Cloud subscription is active.

        :return: True if HA Cloud is logged in and has active subscription.
        """
        # Find the Home Assistant provider
        ha_provider = cast("HomeAssistantProvider | None", self.mass.get_provider("hass"))
        if not ha_provider:
            return False

        try:
            # Access the hass client from the provider
            hass_client = ha_provider.hass
            if not hass_client or not hass_client.connected:
                return False

            # Call cloud/status command to check subscription
            result = await hass_client.send_command("cloud/status")

            # Check for logged_in and active_subscription
            logged_in = result.get("logged_in", False)
            active_subscription = result.get("active_subscription", False)

            return bool(logged_in and active_subscription)

        except Exception:
            return False

    async def _get_ha_cloud_ice_servers(self) -> list[dict[str, str]] | None:
        """Get ICE servers from Home Assistant Cloud.

        :return: List of ICE server configurations or None if unavailable.
        """
        # Find the Home Assistant provider
        ha_provider = cast("HomeAssistantProvider | None", self.mass.get_provider("hass"))
        if not ha_provider:
            return None

        try:
            hass_client = ha_provider.hass
            if not hass_client or not hass_client.connected:
                return None

            # Try to get ICE servers from HA Cloud
            # This might be available via a cloud API endpoint
            # For now, return None and use default STUN servers
            # TODO: Research if HA Cloud exposes ICE/TURN server endpoints
            self.logger.debug(
                "Using default STUN servers (HA Cloud ICE servers not yet implemented)"
            )
            return None

        except Exception:
            self.logger.exception("Error getting Home Assistant Cloud ICE servers")
            return None

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

        @api_command("remote_access/info")
        def get_remote_access_info() -> RemoteAccessInfo:
            """Get remote access information.

            Returns information about the remote access configuration including
            whether it's enabled, running status, connected status, and the Remote ID.
            """
            return RemoteAccessInfo(
                enabled=self.is_enabled,
                running=self.is_running,
                connected=self.is_connected,
                remote_id=self._remote_id or "",
                ha_cloud_available=self._ha_cloud_available,
                signaling_url=SIGNALING_SERVER_URL,
            )

        @api_command("remote_access/configure", required_role="admin")
        async def configure_remote_access(
            enabled: bool,
        ) -> RemoteAccessInfo:
            """
            Configure remote access settings.

            :param enabled: Enable or disable remote access.

            Starts or stops the WebRTC gateway based on the enabled parameter.
            Returns the updated remote access info.
            """
            # Save configuration
            self._enabled = enabled
            self.mass.config.set(f"{CONF_CORE}/{CONF_KEY_MAIN}/{CONF_ENABLED}", enabled)
            allow_start = self._ha_cloud_available and self._enabled

            # Start or stop the gateway based on enabled flag
            if allow_start and not self.is_running:
                await self.start()
            elif not allow_start and self.is_running:
                await self.stop()
            return get_remote_access_info()
