"""
MCP Server Plugin Provider for Music Assistant.

Exposes Music Assistant's library, queue, playback, players, and metadata
controllers as a Model Context Protocol server, accessible to Claude Code,
Codex, and other MCP-aware LLM clients.

The runtime is built on PrefectHQ FastMCP v3 and mounted into MA's existing
aiohttp webserver under ``/mcp/v1`` via an ASGI bridge — no second uvicorn,
no extra port, no changes to MA core.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    from .config import build_config_entries  # noqa: PLC0415

    return build_config_entries(mass, values or {})


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    from .provider import MCPServerProvider  # noqa: PLC0415

    return MCPServerProvider(mass, manifest, config)
