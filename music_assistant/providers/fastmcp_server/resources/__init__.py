"""MCP resource registration entry point."""
# Relative imports are the canonical pattern across MA providers — sync-to-fork
# preserves them verbatim, so disable TID252 file-wide here.
# ruff: noqa: TID252

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..constants import CONF_RES_LIBRARY, CONF_RES_PLAYER
from .library_resources import register_library_resources
from .player_resources import register_player_resources

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.mass import MusicAssistant


def register_resources(mcp: Any, mass: MusicAssistant, config: ProviderConfig) -> None:
    """
    Register MCP resources, gated by config toggles.

    :param mcp: FastMCP root server.
    :param mass: MusicAssistant instance.
    :param config: provider config (controls which resource groups to expose).
    """
    if config.get_value(CONF_RES_LIBRARY):
        register_library_resources(mcp, mass)
    if config.get_value(CONF_RES_PLAYER):
        register_player_resources(mcp, mass)


__all__ = ["register_resources"]
