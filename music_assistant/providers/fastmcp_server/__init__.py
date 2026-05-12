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
from typing import TYPE_CHECKING, Any

__version__ = "0.3.8"

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


def _sanitize_external_base_url(value: str | None) -> str | None:
    """Return ``value`` if it is a plausible ``http(s)://`` base URL, else ``None``.

    Defends against an admin pasting (or a misbehaving proxy injecting) a
    scheme-less or ``javascript:`` URL into the Connect Wizard link, which
    the MA frontend would feed straight to ``window.open``.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate.lower().startswith(("http://", "https://")):
        LOGGER.warning(
            "Connect Wizard: ignoring external base URL with unsupported scheme: %r",
            candidate,
        )
        return None
    return candidate


def _detect_external_base_url(mass: MusicAssistant, current_user: Any) -> str | None:
    """Return the external base URL for the current user's active WS client.

    MA's :class:`WebsocketClientHandler` stores a per-connection ``base_url``
    derived from ``X-Forwarded-Host`` + ``X-Ingress-Path`` — exactly the
    prefix the Connect Wizard needs so ``window.open`` produces a working
    URL under Home Assistant add-on ingress. We pick the client whose
    authenticated user matches the invoker of the action.

    Returns ``None`` when nothing matches (e.g. action invoked outside the
    WS server, or no forward headers were captured).
    """
    if current_user is None:
        return None
    try:
        webserver = getattr(mass, "webserver", None)
        clients = getattr(webserver, "clients", None) or ()
    except Exception:
        return None

    def _user_id(user: Any) -> Any:
        return getattr(user, "user_id", None) or getattr(user, "username", None)

    target = _user_id(current_user)
    for client in clients:
        client_base = getattr(client, "base_url", None)
        if not client_base:
            continue
        client_user = getattr(client, "_authenticated_user", None)
        if client_user is None:
            continue
        if _user_id(client_user) == target:
            return str(client_base)
    return None


async def _dispatch_open_connect(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
) -> None:
    """Mint a wizard bootstrap and signal the wizard URL to the frontend.

    The MA frontend's ``EditProvider`` view subscribes to ``AUTH_SESSION``
    events and ignores anything whose ``object_id`` does not match the
    ``session_id`` it injected into ``values``. We must echo that same id
    back as the event's ``object_id`` so the browser tab actually opens.

    URL resolution order: (1) auto-detect from the active WS client's
    ingress-aware ``base_url``; (2) explicit ``connect_external_url`` config
    override; (3) path-only fallback resolved against the browser's origin.
    """
    from .connect import handle_open_connect_action  # noqa: PLC0415
    from .constants import (  # noqa: PLC0415
        CONF_CONNECT_EXTERNAL_URL,
        CONF_MOUNT_PATH,
        DEFAULT_MOUNT_PATH,
    )

    mount_path = str(values.get(CONF_MOUNT_PATH) or DEFAULT_MOUNT_PATH)
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

    external_base_url = _sanitize_external_base_url(_detect_external_base_url(mass, current_user))
    if not external_base_url:
        external_base_url = _sanitize_external_base_url(
            str(values.get(CONF_CONNECT_EXTERNAL_URL) or "")
        )

    try:
        await handle_open_connect_action(
            mass,
            current_user=current_user,
            mount_path=mount_path,
            session_id=session_id or None,
            external_base_url=external_base_url,
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
