"""MCP Server provider — main PluginProvider implementation.

The provider is a thin lifecycle wrapper over :class:`MCPServerRuntime` from
``server.py``. ``handle_async_init`` constructs the runtime and starts it;
``unload`` shuts it down; ``update_config`` either hot-swaps the tag-filter
middleware (for permission-only changes) or restarts the runtime.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.models.plugin import PluginProvider

from .constants import HOT_SWAPPABLE_KEYS

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

    from .server import MCPServerRuntime


LOGGER = logging.getLogger(__name__)


class MCPServerProvider(PluginProvider):
    """Music Assistant plugin provider wrapping an MCP server runtime."""

    _runtime: MCPServerRuntime | None = None

    async def handle_async_init(self) -> None:
        """Build and start the FastMCP runtime."""
        from .server import MCPServerRuntime  # noqa: PLC0415

        self._runtime = MCPServerRuntime(self.mass, self.config, self.logger)
        await self._runtime.start()

    async def loaded_in_mass(self) -> None:
        """Log the public URL once everything is wired up."""
        if self._runtime is not None:
            self.logger.info("MCP server mounted at %s", self._runtime.public_url)

    async def unload(self, is_removed: bool = False) -> None:
        """Stop the runtime and unmount the HTTP route."""
        if self._runtime is not None:
            await self._runtime.stop()
            self._runtime = None

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Apply config changes — hot-swap when possible, restart otherwise."""
        if self._runtime is None:
            return
        normalized_keys = {k.removeprefix("values/") for k in changed_keys}
        if normalized_keys.issubset(HOT_SWAPPABLE_KEYS):
            self.config = config
            await self._runtime.apply_permission_change(config, normalized_keys)
        else:
            await self._runtime.stop()
            self.config = config
            from .server import MCPServerRuntime  # noqa: PLC0415

            self._runtime = MCPServerRuntime(self.mass, config, self.logger)
            await self._runtime.start()
