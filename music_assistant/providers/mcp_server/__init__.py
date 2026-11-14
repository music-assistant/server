"""
Builtin Model Context Protocol (MCP) server.

Lets AI clients, such as Home Assistant, search music and control playback through a small,
fixed set of tools on the /mcp endpoint of the webserver.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.models.plugin import PluginProvider

from .transport import MCP_PATH, MCPTransport

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MCPServerProvider(mass, manifest, config, set())


class MCPServerProvider(PluginProvider):
    """Serve the Music Assistant MCP tools on the webserver."""

    _unregister_routes: tuple[Callable[[], None], ...] = ()

    async def handle_async_init(self) -> None:
        """Register the MCP endpoint."""
        transport = MCPTransport(self.mass)
        register_route = self.mass.webserver.register_dynamic_route
        self._unregister_routes = (register_route(MCP_PATH, transport.handle_request),)

    async def loaded_in_mass(self) -> None:
        """Offer the MCP server to Home Assistant, when running as add-on."""
        if self.mass.running_as_hass_addon:
            self.mass.create_task(self.mass.discovery.announce_mcp_server(MCP_PATH))

    async def unload(self, is_removed: bool = False) -> None:
        """Remove the MCP endpoint and withdraw the offer to Home Assistant."""
        for unregister_route in self._unregister_routes:
            unregister_route()
        self._unregister_routes = ()
        # a server that stops keeps its offer, it comes back with the same endpoint
        if not self.mass.closing:
            await self.mass.discovery.withdraw_mcp_server()
