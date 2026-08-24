"""
HTTP handlers backing the Connect Wizard endpoints.

Five endpoints are mounted under ``<mount_path>/connect``:

* ``GET  /connect``           — serves the single-page HTML wizard.
* ``GET  /connect/info``      — meta JSON (URLs, default policy, clients).
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

from ._revoke import list_user_tokens, revoke_token_by_id
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
    default_profile_provider: Callable[[], str]
    origin_check: Callable[[web.Request], bool]
    # When True, a trusted reverse proxy's ``X-Forwarded-Proto: https`` (or
    # ``X-Forwarded-Scheme: https``) is accepted as proof the public hop was
    # HTTPS, even though MA's own transport sees plain HTTP. Opt-in: forwarded
    # headers are forgeable by any client that can reach MA directly, so this
    # is only safe behind a proxy that sets the header and strips client copies.
    trust_forwarded_proto: bool = False


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


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


def _is_loopback_host(host: str | None) -> bool:
    """Return True if ``host`` (``Host`` header, may include ``:port``) is loopback."""
    if not host:
        return False
    # Strip optional port; IPv6 literals are bracketed (``[::1]:8095``), so
    # rsplit-on-colon-after-bracket-close is the simplest correct approach.
    if host.startswith("["):
        # ``[::1]`` or ``[::1]:8095``
        end = host.find("]")
        bare = host[: end + 1] if end != -1 else host
    else:
        bare = host.rsplit(":", 1)[0]
    return bare.lower() in _LOOPBACK_HOSTS


def _is_request_via_ha_ingress(request: web.Request) -> bool:
    """
    Return True when MA recognises the request as arriving via HA ingress.

    Home Assistant terminates TLS at its own ``https://ha.example/...``
    front door and forwards the request to MA over a *local* socket, so the
    transport on MA's side is plain HTTP — but the bytes never crossed the
    public network. MA's ``is_request_from_ingress`` helper verifies that
    by checking the trusted ingress socket; we mirror its
    ``ImportError → fail closed`` / ``unexpected → log and fail closed``
    contract here (same pattern as :func:`provider.origins.is_origin_allowed_for_request`).
    """
    try:
        from music_assistant.controllers.webserver.helpers.auth_middleware import (  # noqa: PLC0415
            is_request_from_ingress,
        )
    except ImportError, ModuleNotFoundError:
        # Bare provider venv — MA helper unavailable. Fail closed without noise.
        return False
    except Exception:
        LOGGER.exception("Connect Wizard: unexpected error importing ingress helper")
        return False
    try:
        return bool(is_request_from_ingress(request))
    except Exception:
        LOGGER.exception("Connect Wizard: is_request_from_ingress raised")
        return False


def _forwarded_scheme_is_https(request: web.Request) -> bool:
    """
    Return True if a reverse proxy reports the public hop was HTTPS.

    Reads ``X-Forwarded-Proto`` (and ``X-Forwarded-Scheme``, which Nginx
    Proxy Manager and some others set instead). A proxy that chains through
    several hops sends a comma-separated list (``https, http``); the first
    value is the scheme the *client* used, which is the one we care about.

    Caller must gate this on :attr:`WizardContext.trust_forwarded_proto`:
    these headers are trivially forgeable by any client that can reach MA
    directly, so they are only meaningful behind a trusted proxy.
    """
    for header in ("X-Forwarded-Proto", "X-Forwarded-Scheme"):
        raw = request.headers.get(header)
        if raw and raw.split(",", 1)[0].strip().lower() == "https":
            return True
    return False


def _scheme_guard(ctx: WizardContext, request: web.Request) -> web.Response | None:
    """
    Reject plaintext-http credential traffic from untrusted-transport hosts.

    ``/connect/login`` carries the MA admin password; ``/connect/exchange``
    and ``/connect/token`` carry bootstrap / session tokens. Over plain HTTP
    from a LAN host any of these is sniffable. The guard waves through:

    * **HTTPS** — encrypted end-to-end.
    * **Loopback** — the bytes never leave the box.
    * **Home-Assistant ingress** — HA terminates TLS at its public front
      door and forwards the request to MA over a local socket the HA
      auth-middleware verifies; the public hop *is* HTTPS even though
      MA's transport sees plain HTTP.
    * **Trusted reverse proxy** — when ``trust_forwarded_proto`` is enabled,
      an ``X-Forwarded-Proto: https`` from the proxy is accepted as proof the
      public hop was HTTPS (the standard nginx/Traefik/Caddy/NPM deployment,
      where TLS terminates at the proxy and the proxy-to-MA hop is plain HTTP).

    Everything else gets a JSON 400 the wizard UI can surface to the user.
    """
    if request.scheme == "https":
        return None
    if _is_loopback_host(request.host):
        return None
    if _is_request_via_ha_ingress(request):
        return None
    if ctx.trust_forwarded_proto and _forwarded_scheme_is_https(request):
        return None
    LOGGER.warning(
        "Connect Wizard: refused plaintext credential request to %r from %s "
        "(use HTTPS, or open the wizard via http://localhost, or open the "
        "Music Assistant UI via Home Assistant ingress)",
        request.path,
        request.remote,
    )
    return web.json_response(
        {
            "success": False,
            "error": (
                "Plaintext credential traffic from non-loopback hosts is not "
                "allowed. Use HTTPS, or open the wizard via http://localhost."
            ),
        },
        status=400,
    )


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
                # Belt-and-braces with X-Frame-Options for frame-ancestors,
                # plus a tight script/style/connect/img policy so a future
                # edit that interpolates server-side data into the inline
                # HTML can't turn into stored XSS that reads the per-client
                # tokens cached in sessionStorage.
                "Content-Security-Policy": (
                    "default-src 'none'; "
                    "script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; "
                    "connect-src 'self'; "
                    "img-src 'self' data:; "
                    "frame-ancestors 'none'"
                ),
                # The bootstrap now rides in the URL fragment (so it doesn't
                # appear in access logs), but we still suppress Referer so
                # the GitHub footer link (or any future outbound link) can't
                # leak the path + state of the wizard either.
                "Referrer-Policy": "no-referrer",
            },
        )

    return handler


def make_info(ctx: WizardContext) -> Callable[[web.Request], Any]:
    """Build the ``GET /connect/info`` handler — returns the meta JSON."""

    async def handler(request: web.Request) -> web.Response:
        guard = _origin_guard(ctx, request)
        if guard is not None:
            return guard

        base_url = str(ctx.mass.webserver.base_url or "").rstrip("/")
        mount = "/" + ctx.mount_path.strip("/")
        loopback = _loopback_url(base_url) + mount
        advertised = (base_url + mount) if base_url else loopback
        well_known = "/.well-known/oauth-protected-resource" + mount

        try:
            profile = str(ctx.default_profile_provider())
        except AttributeError, RuntimeError, TypeError, ValueError:
            LOGGER.warning("Connect Wizard: default policy provider failed")
            profile = "Safe queries"

        return web.json_response(
            {
                "mount_path": ctx.mount_path,
                "mcp_url_loopback": loopback,
                "mcp_url_advertised": advertised,
                "default_policy": {"profile": profile},
                "clients": clients_to_json(),
                "well_known_url": well_known,
            },
            headers={"Cache-Control": "no-store"},
        )

    return handler


def make_exchange(ctx: WizardContext) -> Callable[[web.Request], Any]:
    """Build the ``POST /connect/exchange`` handler — bootstrap → session token."""

    async def handler(request: web.Request) -> web.Response:
        guard = _origin_guard(ctx, request) or _scheme_guard(ctx, request)
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

        # Make the bootstrap single-use: revoke it BEFORE minting the session
        # so a partial failure (revoke ok, mint fails) cannot leave both the
        # bootstrap and a session valid. ``get_token_id_from_token`` handles
        # both JWTs and legacy hash tokens; if it cannot resolve a token_id
        # we skip the revoke — no regression vs prior behaviour.
        try:
            bootstrap_id = await ctx.mass.webserver.auth.get_token_id_from_token(bootstrap)
        except Exception:
            LOGGER.exception("Connect Wizard: get_token_id_from_token raised for bootstrap")
            bootstrap_id = None
        if bootstrap_id:
            await revoke_token_by_id(ctx.mass, user, bootstrap_id)

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
        guard = _origin_guard(ctx, request) or _scheme_guard(ctx, request)
        if guard is not None:
            return guard

        body = await _read_json(request)
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        if not username or not password:
            return web.json_response({"error": "missing credentials"}, status=400)

        try:
            # ``Any`` deliberately — MA currently types this as ``dict[str, Any]``
            # but the shape is not part of the documented contract. A future
            # migration to a typed ``LoginResult`` dataclass would otherwise
            # silently flip the success branch to "always invalid credentials".
            result: Any = await ctx.mass.webserver.auth.login(
                username=username,
                password=password,
                provider_id="builtin",
            )
        except Exception:
            LOGGER.exception("Connect Wizard: login raised")
            return web.json_response({"success": False, "error": "login failed"}, status=401)

        def _get(key: str, default: Any = None) -> Any:
            if result is None:
                return default
            if isinstance(result, dict):
                return result.get(key, default)
            return getattr(result, key, default)

        if not _get("success", False):
            err = _get("error", "invalid credentials")
            return web.json_response({"success": False, "error": str(err)}, status=401)

        return web.json_response(
            {
                "success": True,
                "session_token": _get("access_token"),
                "user": _get("user", {}),
            }
        )

    return handler


def make_mint_token(ctx: WizardContext) -> Callable[[web.Request], Any]:
    """Build the ``POST /connect/token`` handler — mint per-client long-lived token."""

    async def handler(request: web.Request) -> web.Response:
        guard = _origin_guard(ctx, request) or _scheme_guard(ctx, request)
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

        new_name = f"MCP — {spec.label}"

        # Server-side dedup: revoke any existing tokens with this exact
        # client-token name for the session user, via the sanctioned
        # auth.get_user_tokens / auth.revoke_token API. Yields typed
        # AuthToken dataclasses — no raw sqlite rows leak in. Idempotent
        # across browser/server restarts: a stale `MCP — <Client>` row
        # from any prior wizard session is reclaimed before the new mint.
        for tok in await list_user_tokens(ctx.mass, user):
            if tok.name == new_name:
                await revoke_token_by_id(ctx.mass, user, tok.token_id)

        try:
            token = await ctx.mass.webserver.auth.create_token(
                user=user,
                name=new_name,
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
