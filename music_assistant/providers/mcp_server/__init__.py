"""
Builtin Model Context Protocol (MCP) server.

Lets AI clients, such as Home Assistant, search music and control playback through a small,
fixed set of tools on the /mcp endpoint of the webserver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.plugin import PluginProvider

from .transport import MCP_PATH, MCPTransport

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_EXPOSE_TO_HOME_ASSISTANT: Final = "expose_to_home_assistant"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MCPServerProvider(mass, manifest, config, set())


class MCPServerProvider(PluginProvider):
    """Serve the Music Assistant MCP tools on the webserver."""

    _unregister_route: Callable[[], None] | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries for this provider."""
        return (
            ConfigEntry(
                key=CONF_EXPOSE_TO_HOME_ASSISTANT,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                hidden=not self.mass.running_as_hass_addon,
            ),
        )

    async def handle_async_init(self) -> None:
        """Register the MCP endpoint."""
        self._unregister_route = self.mass.webserver.register_dynamic_route(
            MCP_PATH, MCPTransport(self.mass).handle_request
        )

    async def loaded_in_mass(self) -> None:
        """Offer the MCP server to Home Assistant, when enabled."""
        if self.mass.running_as_hass_addon and self.config.get_value(CONF_EXPOSE_TO_HOME_ASSISTANT):
            self.mass.create_task(self.mass.discovery.announce_mcp_server(MCP_PATH))

    async def unload(self, is_removed: bool = False) -> None:
        """Remove the MCP endpoint and withdraw the offer to Home Assistant."""
        if self._unregister_route:
            self._unregister_route()
            self._unregister_route = None
        # a server that stops keeps its offer, it comes back with the same endpoint
        if not self.mass.closing:
            await self.mass.discovery.withdraw_mcp_server()
