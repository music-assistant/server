"""
MCP Server provider — main PluginProvider implementation.

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
    from music_assistant_models.config_entries import (
        ConfigActionResult,
        ConfigEntry,
        ProviderConfig,
    )

    from .server import MCPServerRuntime


LOGGER = logging.getLogger(__name__)


class MCPServerProvider(PluginProvider):
    """Music Assistant plugin provider wrapping an MCP server runtime."""

    _runtime: MCPServerRuntime | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        from .config import build_config_entries  # noqa: PLC0415
        from .constants import CONF_MOUNT_PATH, DEFAULT_MOUNT_PATH  # noqa: PLC0415

        return build_config_entries(
            self.mass, str(self.get_config_value(CONF_MOUNT_PATH, DEFAULT_MOUNT_PATH))
        )

    async def handle_config_action(
        self, action: str
    ) -> tuple[ConfigEntry, ...] | ConfigActionResult | None:
        """Handle a one-shot config action button press and report its outcome."""
        if action == "open_connect":
            from music_assistant_models.config_entries import ConfigActionResult  # noqa: PLC0415
            from music_assistant_models.errors import ActionUnavailable  # noqa: PLC0415

            from ._init_helpers import _dispatch_open_connect  # noqa: PLC0415
            from .constants import CONF_CONNECT_EXTERNAL_URL, CONF_MOUNT_PATH  # noqa: PLC0415

            url = await _dispatch_open_connect(
                self.mass,
                {
                    CONF_MOUNT_PATH: self.get_config_value(CONF_MOUNT_PATH),
                    CONF_CONNECT_EXTERNAL_URL: self.get_config_value(CONF_CONNECT_EXTERNAL_URL),
                },
            )
            if url is None:
                raise ActionUnavailable(
                    "The Connect Wizard could not be opened; check the provider logs.",
                    translation_key="connect_wizard_unavailable",
                    translation_owner=self.translation_owner,
                )
            return ConfigActionResult(open_url=url)
        return await super().handle_config_action(action)

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
