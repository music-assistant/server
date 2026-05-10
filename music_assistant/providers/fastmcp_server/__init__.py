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

__version__ = "0.3.4"

LOGGER = logging.getLogger(__name__)

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
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    When ``action == "open_connect"`` is dispatched, mint a bootstrap token
    bound to the calling user (when available) and signal MA's frontend to
    open the Connect Wizard URL — the entries themselves are returned
    unchanged so the settings panel re-renders cleanly.
    """
    from .config import build_config_entries  # noqa: PLC0415

    if action == "open_connect":
        await _dispatch_open_connect(mass, values or {})

    return build_config_entries(mass, values or {})


async def _dispatch_open_connect(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
) -> None:
    """Mint a wizard bootstrap and signal the wizard URL to the frontend.

    The MA frontend's ``EditProvider`` view subscribes to ``AUTH_SESSION``
    events and ignores anything whose ``object_id`` does not match the
    ``session_id`` it injected into ``values``. We must echo that same id
    back as the event's ``object_id`` so the browser tab actually opens.
    """
    from .connect import handle_open_connect_action  # noqa: PLC0415
    from .constants import CONF_MOUNT_PATH, DEFAULT_MOUNT_PATH  # noqa: PLC0415

    mount_path = str(values.get(CONF_MOUNT_PATH) or DEFAULT_MOUNT_PATH)
    base_url = str(getattr(mass.webserver, "base_url", "") or "")
    session_id = str(values.get("session_id") or "")

    current_user: object | None = None
    try:
        from music_assistant.controllers.webserver.helpers.auth_middleware import (  # noqa: PLC0415
            get_current_user,
        )

        current_user = get_current_user()
    except Exception:
        LOGGER.debug("Connect Wizard: get_current_user lookup failed", exc_info=True)
        current_user = None

    try:
        await handle_open_connect_action(
            mass,
            current_user=current_user,
            mount_path=mount_path,
            base_url=base_url,
            session_id=session_id or None,
        )
    except Exception:
        LOGGER.exception("Connect Wizard: open_connect action failed")


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    from .provider import MCPServerProvider  # noqa: PLC0415

    return MCPServerProvider(mass, manifest, config)
