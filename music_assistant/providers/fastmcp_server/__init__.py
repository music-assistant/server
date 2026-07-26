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

import logging
from typing import TYPE_CHECKING

from ._init_helpers import (
    _detect_external_base_url,
    _dispatch_open_connect,
    _sanitize_external_base_url,
)

LOGGER = logging.getLogger(__name__)

# Re-export for callers that historically imported these helpers from the
# package root. New code (and tests) should import from
# ``provider._init_helpers`` directly — the upstream-PR rewrite only
# translates dotted ``from provider.<sub> import …`` forms.
__all__ = [
    "_detect_external_base_url",
    "_dispatch_open_connect",
    "_sanitize_external_base_url",
    "setup",
]

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    from .provider import MCPServerProvider  # noqa: PLC0415

    return MCPServerProvider(mass, manifest, config)
