"""HTTP handlers backing the Connect Wizard endpoints.

Five endpoints are mounted under ``<mount_path>/connect``:

* ``GET  /connect``           — serves the single-page HTML wizard.
* ``GET  /connect/info``      — meta JSON (URLs, version, enabled permissions, clients).
* ``POST /connect/exchange``  — exchanges a bootstrap token for a session token.
* ``POST /connect/login``     — username/password login fallback.
* ``POST /connect/token``     — mints a per-client long-lived token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aiohttp import web

from .clients import clients_to_json, lookup_client
from .page import HTML

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WizardContext:
    """Shared state captured at mount time and passed to every handler."""

    mass: MusicAssistant
    mount_path: str
    version: str
    enabled_tags_provider: Callable[[], list[str]]
    origin_check: Callable[[web.Request], bool]


def _origin_guard(ctx: WizardContext, request: web.Request) -> web.Response | None:
    """Return a 403 response if the request's ``Origin`` is not allowlisted."""
    if not ctx.origin_check(request):
        LOGGER.warning(
            "Connect Wizard: rejected request with Origin=%r from %s",
            request.headers.get("Origin"),
            request.remote,
        )
        return web.Response(status=403, text="Forbidden Origin")
    return None


async def _read_json(request: web.Request) -> dict[str, Any]:
    """Best-effort JSON parse; missing/malformed body becomes an empty dict."""
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def make_serve_page(_ctx: WizardContext) -> Callable[[web.Request], Any]:
    """Build the ``GET /connect`` handler — serves the wizard HTML page."""

    async def handler(_request: web.Request) -> web.Response:
        # Origin check intentionally skipped on the page itself: browsers don't
        # send Origin on top-level navigation. The /connect/* JSON endpoints
        # do enforce it.
        return web.Response(
            body=HTML.encode("utf-8"),
            content_type="text/html",
            charset="utf-8",
            headers={
                "Cache-Control": "no-store",
                # The wizard mints long-lived MA tokens on user click. Refuse
                # to be framed so a hostile page cannot UI-redress the user
                # into pressing "Generate config" inside an invisible iframe.
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "frame-ancestors 'none'",
            },
        )

    return handler


def make_info(ctx: WizardContext) -> Callable[[web.Request], Any]:
    """Build the ``GET /connect/info`` handler — returns the meta JSON."""

    async def handler(request: web.Request) -> web.Response:
        guard = _origin_guard(ctx, request)
        if guard is not None:
            return guard

        base_url = str(getattr(ctx.mass.webserver, "base_url", "") or "").rstrip("/")
        mount = "/" + ctx.mount_path.strip("/")
        loopback = _loopback_url(base_url) + mount
        advertised = (base_url + mount) if base_url else loopback
        well_known = "/.well-known/oauth-protected-resource" + mount

        try:
            permissions = list(ctx.enabled_tags_provider() or [])
        except Exception:
            LOGGER.exception("Connect Wizard: enabled_tags_provider raised")
            permissions = []

        return web.json_response(
            {
                "version": ctx.version,
                "mount_path": ctx.mount_path,
                "mcp_url_loopback": loopback,
                "mcp_url_advertised": advertised,
                "permissions": permissions,
                "clients": clients_to_json(),
                "well_known_url": well_known,
            },
            headers={"Cache-Control": "no-store"},
        )

    return handler


def make_exchange(ctx: WizardContext) -> Callable[[web.Request], Any]:
    """Build the ``POST /connect/exchange`` handler — bootstrap → session token."""

    async def handler(request: web.Request) -> web.Response:
        guard = _origin_guard(ctx, request)
        if guard is not None:
            return guard

        body = await _read_json(request)
        bootstrap = str(body.get("bootstrap") or "")
        if not bootstrap:
            return web.json_response({"error": "missing bootstrap"}, status=400)

        try:
            user = await ctx.mass.webserver.auth.authenticate_with_token(bootstrap)
        except Exception:
            LOGGER.exception("Connect Wizard: bootstrap verify raised")
            return web.json_response({"error": "verify failed"}, status=401)

        if user is None or not getattr(user, "enabled", True):
            return web.json_response({"error": "invalid bootstrap"}, status=401)

        try:
            session = await ctx.mass.webserver.auth.create_token(
                user=user,
                name="MCP — wizard session",
                is_long_lived=False,
            )
        except Exception:
            LOGGER.exception("Connect Wizard: session token mint failed")
            return web.json_response({"error": "mint failed"}, status=500)

        return web.json_response(
            {
                "session_token": session,
                "user": _public_user(user),
            }
        )

    return handler


def make_login(ctx: WizardContext) -> Callable[[web.Request], Any]:
    """Build the ``POST /connect/login`` handler — username/password fallback."""

    async def handler(request: web.Request) -> web.Response:
        guard = _origin_guard(ctx, request)
        if guard is not None:
            return guard

        body = await _read_json(request)
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        if not username or not password:
            return web.json_response({"error": "missing credentials"}, status=400)

        try:
            result = await ctx.mass.webserver.auth.login(
                username=username,
                password=password,
                provider_id="builtin",
            )
        except Exception:
            LOGGER.exception("Connect Wizard: login raised")
            return web.json_response({"success": False, "error": "login failed"}, status=401)

        if not isinstance(result, dict) or not result.get("success"):
            err = (
                result.get("error", "invalid credentials")
                if isinstance(result, dict)
                else "invalid credentials"
            )
            return web.json_response({"success": False, "error": str(err)}, status=401)

        return web.json_response(
            {
                "success": True,
                "session_token": result.get("access_token"),
                "user": result.get("user", {}),
            }
        )

    return handler


def make_mint_token(ctx: WizardContext) -> Callable[[web.Request], Any]:
    """Build the ``POST /connect/token`` handler — mint per-client long-lived token."""

    async def handler(request: web.Request) -> web.Response:
        guard = _origin_guard(ctx, request)
        if guard is not None:
            return guard

        body = await _read_json(request)
        session_token = str(body.get("session_token") or "")
        client_id = str(body.get("client_id") or "")
        if not session_token or not client_id:
            return web.json_response({"error": "missing fields"}, status=400)

        spec = lookup_client(client_id)
        if spec is None:
            return web.json_response({"error": f"unknown client {client_id!r}"}, status=400)

        try:
            user = await ctx.mass.webserver.auth.authenticate_with_token(session_token)
        except Exception:
            LOGGER.exception("Connect Wizard: session verify raised")
            return web.json_response({"error": "session invalid"}, status=401)

        if user is None or not getattr(user, "enabled", True):
            return web.json_response({"error": "session invalid"}, status=401)

        try:
            token = await ctx.mass.webserver.auth.create_token(
                user=user,
                name=f"MCP — {spec.label}",
                is_long_lived=True,
            )
        except Exception:
            LOGGER.exception("Connect Wizard: per-client token mint failed")
            return web.json_response({"error": "mint failed"}, status=500)

        return web.json_response({"token": token})

    return handler


# ── helpers ──────────────────────────────────────────────────────────────────


def _loopback_url(base_url: str) -> str:
    """Return ``scheme://localhost[:port]`` derived from ``base_url``."""
    if not base_url:
        return "http://localhost"
    parts = urlsplit(base_url)
    scheme = parts.scheme or "http"
    port = parts.port
    suffix = f":{port}" if port else ""
    return f"{scheme}://localhost{suffix}"


def _public_user(user: Any) -> dict[str, Any]:
    """Project a User object onto the small set of fields the wizard UI uses."""
    return {
        "user_id": str(getattr(user, "user_id", "") or ""),
        "username": str(getattr(user, "username", "") or ""),
        "role": str(getattr(getattr(user, "role", None), "value", getattr(user, "role", "")) or ""),
    }
