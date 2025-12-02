"""
Remote Access Plugin Provider for Music Assistant.

This plugin manages WebRTC-based remote access to Music Assistant instances.
It connects to a signaling server and handles incoming WebRTC connections,
bridging them to the local WebSocket API.

Requires an active Home Assistant Cloud subscription for the best experience.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, EventType, ProviderFeature

from music_assistant.constants import CONF_ENABLED
from music_assistant.helpers.api import api_command
from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.remote_access.gateway import WebRTCGateway, generate_remote_id

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.hass import HomeAssistantProvider

# Signaling server URL
SIGNALING_SERVER_URL = "wss://signaling.music-assistant.io/ws"

# Config keys
CONF_REMOTE_ID = "remote_id"

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RemoteAccessProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    :param mass: MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    entries: list[ConfigEntry] = [
        ConfigEntry(
            key=CONF_REMOTE_ID,
            type=ConfigEntryType.STRING,
            label="Remote ID",
            description="Unique identifier for WebRTC remote access. "
            "Generated automatically and should not be changed.",
            required=False,
            hidden=True,
        )
        # TODO: Add a message that optimal experience requires Home Assistant Cloud subscription
    ]
    # Get the remote ID if instance exists
    remote_id: str | None = None
    if instance_id:
        if remote_id_value := mass.config.get_raw_provider_config_value(
            instance_id, CONF_REMOTE_ID
        ):
            remote_id = str(remote_id_value)
            entries += [
                ConfigEntry(
                    key="remote_access_id_intro",
                    type=ConfigEntryType.LABEL,
                    label="Remote access is enabled. You can securely connect to your "
                    "Music Assistant instance from https://app.music-assistant.io or supported "
                    "(mobile) apps using the Remote ID below.",
                    hidden=False,
                ),
                ConfigEntry(
                    key="remote_access_id_label",
                    type=ConfigEntryType.LABEL,
                    label=f"Remote Access ID: {remote_id}",
                    hidden=False,
                ),
            ]

    return tuple(entries)


async def _check_ha_cloud_status(mass: MusicAssistant) -> bool:
    """Check if Home Assistant Cloud subscription is active.

    :param mass: MusicAssistant instance.
    :return: True if HA Cloud is logged in and has active subscription.
    """
    # Find the Home Assistant provider
    ha_provider = cast("HomeAssistantProvider | None", mass.get_provider("hass"))
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


class RemoteAccessProvider(PluginProvider):
    """Plugin Provider for WebRTC-based remote access."""

    gateway: WebRTCGateway | None = None
    _remote_id: str | None = None

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        remote_id_value = self.config.get_value(CONF_REMOTE_ID)
        if not remote_id_value:
            # First time setup, generate a new Remote ID
            remote_id_value = generate_remote_id()
            self._remote_id = remote_id_value
            self.logger.debug("Generated new Remote ID: %s", remote_id_value)
            await self.mass.config.save_provider_config(
                self.domain,
                {
                    CONF_ENABLED: True,
                    CONF_REMOTE_ID: remote_id_value,
                },
                instance_id=self.instance_id,
            )

        else:
            self._remote_id = str(remote_id_value)

        # Register API commands
        self._register_api_commands()

        # Subscribe to provider updates to check for Home Assistant Cloud
        self.mass.subscribe(self._on_providers_updated, EventType.PROVIDERS_UPDATED)

        # Try initial setup (providers might already be loaded)
        await self._try_enable_remote_access()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider.

        :param is_removed: True when the provider is removed from the configuration.
        """
        if self.gateway:
            await self.gateway.stop()
            self.gateway = None
            self.logger.debug("WebRTC Remote Access stopped")

    def _on_remote_id_ready(self, remote_id: str) -> None:
        """Handle Remote ID registration with signaling server.

        :param remote_id: The registered Remote ID.
        """
        self.logger.debug("Remote ID registered with signaling server: %s", remote_id)
        self._remote_id = remote_id

    def _on_providers_updated(self, event: MassEvent) -> None:
        """Handle providers updated event.

        :param event: The providers updated event.
        """
        if self.gateway is not None:
            # Already set up, no need to check again
            return
        # Try to enable remote access when providers are updated
        self.mass.create_task(self._try_enable_remote_access())

    async def _try_enable_remote_access(self) -> None:
        """Try to enable remote access if Home Assistant Cloud is available."""
        if self.gateway is not None:
            # Already set up
            return

        # Determine local WebSocket URL from webserver config
        base_url = self.mass.webserver.base_url
        local_ws_url = base_url.replace("http", "ws")
        if not local_ws_url.endswith("/"):
            local_ws_url += "/"
        local_ws_url += "ws"

        # Get ICE servers from HA Cloud if available
        ice_servers: list[dict[str, str]] | None = None
        if await _check_ha_cloud_status(self.mass):
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
            on_remote_id_ready=self._on_remote_id_ready,
            ice_servers=ice_servers,
        )

        await self.gateway.start()
        self.logger.info("WebRTC Remote Access enabled - Remote ID: %s", self._remote_id)

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
