"""
Builtin Model Context Protocol (MCP) server.

Lets AI clients, such as Home Assistant, search music and control playback through a small,
fixed set of tools on the /mcp endpoint of the webserver.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Final

from aiohttp import web
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

# the secret in the path of the private endpoint that only Home Assistant learns,
# through the Supervisor discovery
CONF_HOME_ASSISTANT_SECRET: Final = "home_assistant_secret"  # noqa: S105 - config key
# the paths an MCP client probes for OAuth metadata after a 401, which would otherwise end up
# in the log of unhandled requests, the secret included
WELL_KNOWN_PROBE_PATHS: Final = (
    "/.well-known/oauth-protected-resource{path}",
    "/.well-known/oauth-authorization-server{path}",
    "/.well-known/openid-configuration{path}",
    "{path}/.well-known/openid-configuration",
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MCPServerProvider(mass, manifest, config, set())


class MCPServerProvider(PluginProvider):
    """Serve the Music Assistant MCP tools on the webserver."""

    _unregister_routes: tuple[Callable[[], None], ...] = ()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries for this provider."""
        return (
            ConfigEntry(
                key=CONF_HOME_ASSISTANT_SECRET,
                type=ConfigEntryType.SECURE_STRING,
                required=False,
                hidden=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Register the MCP endpoints."""
        # Home Assistant 2026.12 and newer send the token of the Supervisor discovery as bearer
        # token. Older versions read only the url and send no Authorization header, so for
        # them the secret path plus the host check (auth.py) stand in for that token, until
        # this path goes away (music-assistant/backlog#382).
        secret = self.get_config_value(CONF_HOME_ASSISTANT_SECRET, return_type=str)
        if not secret:
            secret = secrets.token_urlsafe(24)
            self._update_config_value(
                CONF_HOME_ASSISTANT_SECRET, secret, encrypted=True, immediate=True
            )
        self._home_assistant_path = f"{MCP_PATH}/{secret}"
        transport = MCPTransport(self.mass)
        register_route = self.mass.webserver.register_dynamic_route
        self._unregister_routes = (
            register_route(MCP_PATH, transport.handle_request),
            register_route(self._home_assistant_path, transport.handle_home_assistant_request),
            *(
                register_route(probe_path.format(path=self._home_assistant_path), _not_found)
                for probe_path in WELL_KNOWN_PROBE_PATHS
            ),
        )

    async def loaded_in_mass(self) -> None:
        """Offer the MCP server to Home Assistant, when running as add-on."""
        if self.mass.running_as_hass_addon:
            self.mass.create_task(
                self.mass.discovery.announce_mcp_server(self._home_assistant_path)
            )

    async def unload(self, is_removed: bool = False) -> None:
        """Remove the MCP endpoints and withdraw the offer to Home Assistant."""
        for unregister_route in self._unregister_routes:
            unregister_route()
        self._unregister_routes = ()
        # a server that stops keeps its offer, it comes back with the same endpoint
        if not self.mass.closing:
            await self.mass.discovery.withdraw_mcp_server()


async def _not_found(_request: web.Request) -> web.Response:
    """Answer a request with a plain 404."""
    return web.Response(status=404)
