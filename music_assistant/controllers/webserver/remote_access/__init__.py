"""Remote Access manager for Music Assistant webserver."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.controllers.webserver.remote_access.gateway import (
    WebRTCGateway,
    generate_remote_id,
)
from music_assistant.helpers.api import api_command

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant.controllers.webserver import WebserverController

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.remote_access")

# Signaling server URL
SIGNALING_SERVER_URL = "wss://signaling.music-assistant.io/ws"

# Internal config key for storing the remote ID
_CONF_REMOTE_ID = "remote_id"


class RemoteAccessManager:
    """Manager for WebRTC-based remote access (part of webserver controller)."""

    def __init__(self, webserver: WebserverController) -> None:
        """Initialize the remote access manager.

        :param webserver: WebserverController instance.
        """
        self.webserver = webserver
        self.mass = webserver.mass
        self.logger = LOGGER
        self.gateway: WebRTCGateway | None = None
        self._remote_id: str | None = None
        self._setup_done = False

    async def setup(self) -> None:
        """Initialize the remote access manager."""
        self.logger.debug("RemoteAccessManager.setup() called")

        # Register API commands immediately
        self._register_api_commands()

        # Subscribe to provider updates to check for Home Assistant Cloud
        self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED)

        # Try initial setup (providers might already be loaded)
        await self._try_enable_remote_access()

    async def close(self) -> None:
        """Cleanup on exit."""
        if self.gateway:
            await self.gateway.stop()
            self.gateway = None
            self.logger.info("WebRTC Remote Access stopped")

    def _on_remote_id_ready(self, remote_id: str) -> None:
        """Handle Remote ID registration with signaling server.

        :param remote_id: The registered Remote ID.
        """
        self.logger.info("Remote ID registered with signaling server: %s", remote_id)
        self._remote_id = remote_id

    def _on_providers_updated(self, event: MassEvent) -> None:
        """Handle providers updated event.

        :param event: The providers updated event.
        """
        if self._setup_done:
            # Already set up, no need to check again
            return
        # Try to enable remote access when providers are updated
        self.mass.create_task(self._try_enable_remote_access())

    async def _try_enable_remote_access(self) -> None:
        """Try to enable remote access if Home Assistant Cloud is available."""
        if self._setup_done:
            # Already set up
            return

        # Check if Home Assistant Cloud is available and active
        cloud_status = await self._check_ha_cloud_status()
        if not cloud_status:
            self.logger.debug("Home Assistant Cloud not available yet")
            return

        # Mark as done to prevent multiple attempts
        self._setup_done = True
        self.logger.info("Home Assistant Cloud subscription detected, enabling remote access")

        # Get or generate Remote ID
        remote_id_value = self.webserver.config.get_value(_CONF_REMOTE_ID)
        if not remote_id_value:
            # Generate new Remote ID and save it
            remote_id_value = generate_remote_id()
            # Save the Remote ID to config
            self.mass.config.set_raw_core_config_value(
                "webserver", _CONF_REMOTE_ID, remote_id_value
            )
            self.mass.config.save(immediate=True)
            self.logger.info("Generated new Remote ID: %s", remote_id_value)

        # Ensure remote_id is a string
        if isinstance(remote_id_value, str):
            self._remote_id = remote_id_value
        else:
            self.logger.error("Invalid remote_id type: %s", type(remote_id_value))
            return

        # Determine local WebSocket URL
        bind_port_value = self.webserver.config.get_value("bind_port", 8095)
        bind_port = int(bind_port_value) if isinstance(bind_port_value, int) else 8095
        local_ws_url = f"ws://localhost:{bind_port}/ws"

        # Get ICE servers from HA Cloud if available
        ice_servers = await self._get_ha_cloud_ice_servers()

        # Initialize and start the WebRTC gateway
        self.gateway = WebRTCGateway(
            signaling_url=SIGNALING_SERVER_URL,
            local_ws_url=local_ws_url,
            remote_id=self._remote_id,
            on_remote_id_ready=self._on_remote_id_ready,
            ice_servers=ice_servers,
        )

        await self.gateway.start()
        self.logger.info("WebRTC Remote Access enabled - Remote ID: %s", self._remote_id)

    async def _check_ha_cloud_status(self) -> bool:
        """Check if Home Assistant Cloud subscription is active.

        :return: True if HA Cloud is logged in and has active subscription.
        """
        # Find the Home Assistant provider
        ha_provider = None
        for provider in self.mass.providers:
            if provider.domain == "hass" and provider.available:
                ha_provider = provider
                break

        if not ha_provider:
            self.logger.debug("Home Assistant provider not found or not available")
            return False

        try:
            # Access the hass client from the provider
            if not hasattr(ha_provider, "hass"):
                self.logger.debug("Provider does not have hass attribute")
                return False
            hass_client = ha_provider.hass  # type: ignore[union-attr]
            if not hass_client or not hass_client.connected:
                self.logger.debug("Home Assistant client not connected")
                return False

            # Call cloud/status command to check subscription
            result = await hass_client.send_command("cloud/status")

            # Check for logged_in and active_subscription
            logged_in = result.get("logged_in", False)
            active_subscription = result.get("active_subscription", False)

            if logged_in and active_subscription:
                self.logger.info("Home Assistant Cloud subscription is active")
                return True

            self.logger.info(
                "Home Assistant Cloud not active (logged_in=%s, active_subscription=%s)",
                logged_in,
                active_subscription,
            )
            return False

        except Exception:
            self.logger.exception("Error checking Home Assistant Cloud status")
            return False

    async def _get_ha_cloud_ice_servers(self) -> list[dict[str, str]] | None:
        """Get ICE servers from Home Assistant Cloud.

        :return: List of ICE server configurations or None if unavailable.
        """
        # Find the Home Assistant provider
        ha_provider = None
        for provider in self.mass.providers:
            if provider.domain == "hass" and provider.available:
                ha_provider = provider
                break

        if not ha_provider:
            return None

        try:
            # Access the hass client from the provider
            if not hasattr(ha_provider, "hass"):
                return None
            hass_client = ha_provider.hass  # type: ignore[union-attr]
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
        def get_remote_access_info() -> dict[str, str | bool]:
            """Get remote access information.

            Returns information about the remote access configuration including
            whether it's enabled, connected status, and the Remote ID for connecting.
            """
            return {
                "enabled": self.is_enabled,
                "connected": self.is_connected,
                "remote_id": self._remote_id or "",
                "signaling_url": SIGNALING_SERVER_URL,
            }
