"""
HTTP handlers for Yandex Smart Home direct connection.

Registers dynamic routes on the MA webserver to handle Yandex Smart Home API
requests directly (without the yaha-cloud.ru WebSocket relay).

Routes:
  Health:
    HEAD/GET /api/yandex_smarthome/v1.0       — health check
    GET      /api/yandex_smarthome/v1.0/ping   — health check

  API (Bearer auth required):
    POST /api/yandex_smarthome/v1.0/user/devices        — list devices
    POST /api/yandex_smarthome/v1.0/user/devices/query   — query states
    POST /api/yandex_smarthome/v1.0/user/devices/action  — execute actions
    POST /api/yandex_smarthome/v1.0/user/unlink          — user unlink

  OAuth (account linking):
    GET  /api/yandex_smarthome/auth/authorize  — authorization page
    POST /api/yandex_smarthome/auth/token      — token exchange
"""

from __future__ import annotations

import html as html_module
import logging
import secrets
import time
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .constants import (
    DIRECT_API_BASE_PATH,
    DIRECT_AUTH_BASE_PATH,
    DIRECT_HEALTH_RESPONSE,
    DIRECT_OAUTH_CLIENT_ID,
    MAX_PENDING_CODES,
    OAUTH_CODE_EXPIRY,
)
from .handlers import (
    build_response,
    handle_device_list,
    handle_devices_action,
    handle_devices_query,
    handle_user_unlink,
    parse_action_payload,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_LOGGER = logging.getLogger(__name__)

# Minimal HTML for the OAuth authorize page
_AUTHORIZE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Music Assistant — Yandex Smart Home</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
           display: flex; justify-content: center; align-items: center;
           min-height: 100vh; margin: 0; background: #f5f5f5; }}
    .card {{ background: white; padding: 2rem; border-radius: 12px;
             box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center;
             max-width: 400px; }}
    h1 {{ font-size: 1.3rem; margin-bottom: 0.5rem; }}
    p {{ color: #666; font-size: 0.9rem; }}
    .btn {{ display: inline-block; padding: 12px 32px; background: #5c6bc0;
            color: white; text-decoration: none; border-radius: 8px;
            font-size: 1rem; margin-top: 1rem; border: none; cursor: pointer; }}
    .btn:hover {{ background: #3f51b5; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🎵 Music Assistant</h1>
    <p>Привязать аккаунт к Яндекс Умному Дому?</p>
    <a class="btn" href="{redirect_url}">Привязать</a>
  </div>
</body>
</html>"""


class DirectConnectionHandler:
    """Handles Yandex Smart Home HTTP requests for direct connection mode."""

    def __init__(
        self,
        mass: MusicAssistant,
        user_id: str,
        access_token: str,
        client_secret: str,
        exposed_ids: set[str] | None = None,
        logger: logging.Logger | None = None,
        on_token_created: Callable[[str], None] | None = None,
        playlist_uris: tuple[str, ...] | list[str] = (),
    ) -> None:
        """
        Initialize the handler.

        :param mass: MusicAssistant instance.
        :param user_id: User identifier for Yandex API responses.
        :param access_token: Current Bearer access token (may be empty
            on first run).
        :param client_secret: OAuth client secret for account linking
            validation.
        :param exposed_ids: Set of player IDs to expose, or ``None`` for
            all.
        :param logger: Optional logger instance.
        :param on_token_created: Callback invoked with new access token
            when generated via OAuth flow (to persist in config).
        """
        self._mass = mass
        self._user_id = user_id
        self._access_token = access_token
        self._client_secret = client_secret
        self._exposed_ids = exposed_ids
        self._playlist_uris = tuple(playlist_uris)
        self._logger = logger or _LOGGER
        self._on_token_created = on_token_created
        self._unregister_callbacks: list[Callable[[], None]] = []
        # Pending OAuth authorization codes: {code: expiry_timestamp}
        self._pending_codes: dict[str, float] = {}

    @property
    def access_token(self) -> str:
        """Return the current access token."""
        return self._access_token

    def register_routes(self) -> None:
        """Register all HTTP routes on the MA webserver."""
        base = DIRECT_API_BASE_PATH
        auth_base = DIRECT_AUTH_BASE_PATH
        register = self._mass.webserver.register_dynamic_route

        # Health check endpoints (no auth)
        routes: list[tuple[str, str, Any]] = [
            (f"{base}", "HEAD", self._handle_health),
            (f"{base}", "GET", self._handle_health),
            (f"{base}/ping", "GET", self._handle_health),
            # API endpoints (auth required)
            (f"{base}/user/devices", "POST", self._handle_devices),
            (f"{base}/user/devices/query", "POST", self._handle_query),
            (f"{base}/user/devices/action", "POST", self._handle_action),
            (f"{base}/user/unlink", "POST", self._handle_unlink),
            # Also support GET for devices (Yandex may use either)
            (f"{base}/user/devices", "GET", self._handle_devices),
            # OAuth account linking endpoints (no auth)
            (f"{auth_base}/authorize", "GET", self._handle_oauth_authorize),
            (f"{auth_base}/token", "POST", self._handle_oauth_token),
        ]

        for path, method, handler in routes:
            try:
                unregister = register(path, handler, method)
            except RuntimeError:
                self._logger.error("Failed to register route %s %s; rolling back", method, path)
                self.unregister_routes()
                raise
            self._unregister_callbacks.append(unregister)

        self._logger.info(
            "Direct connection: registered %d routes on MA webserver",
            len(self._unregister_callbacks),
        )

    def unregister_routes(self) -> None:
        """Unregister all HTTP routes from the MA webserver."""
        for cb in self._unregister_callbacks:
            try:
                cb()
            except Exception:
                self._logger.debug("Error unregistering route", exc_info=True)
        count = len(self._unregister_callbacks)
        self._unregister_callbacks.clear()
        self._logger.info("Direct connection: unregistered %d routes", count)

    # -------------------------------------------------------------------
    # Auth helpers
    # -------------------------------------------------------------------

    def _validate_auth(self, request: web.Request) -> bool:
        """Validate Bearer token from Authorization header."""
        if not self._access_token:
            return False
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return secrets.compare_digest(auth[7:], self._access_token)
        return False

    def _unauthorized_response(self, request_id: str = "") -> web.Response:
        """Return a 401 Unauthorized response in the Smart Home API envelope."""
        return web.json_response(
            build_response(request_id, {"error": "unauthorized"}),
            status=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    # -------------------------------------------------------------------
    # Health check
    # -------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Handle health check (HEAD/GET /v1.0 and /v1.0/ping)."""
        if request.method == "HEAD":
            return web.Response(status=200)
        return web.Response(text=DIRECT_HEALTH_RESPONSE, status=200)

    # -------------------------------------------------------------------
    # Smart Home API endpoints
    # -------------------------------------------------------------------

    async def _handle_devices(self, request: web.Request) -> web.Response:
        """Handle /user/devices — list all exposed devices."""
        request_id = request.headers.get("X-Request-Id", "")
        if not self._validate_auth(request):
            return self._unauthorized_response(request_id)

        try:
            device_list = await handle_device_list(
                self._mass,
                self._user_id,
                exposed_ids=self._exposed_ids,
                playlist_uris=self._playlist_uris,
            )
            return web.json_response(build_response(request_id, asdict(device_list)))
        except Exception:
            self._logger.exception("Error handling /user/devices")
            return web.json_response(build_response(request_id, {}), status=500)

    async def _handle_query(self, request: web.Request) -> web.Response:
        """Handle /user/devices/query — return device states."""
        request_id = request.headers.get("X-Request-Id", "")
        if not self._validate_auth(request):
            return self._unauthorized_response(request_id)

        try:
            body = await request.json()
        except Exception:
            return web.json_response(build_response(request_id, {}), status=400)

        try:
            if not isinstance(body, dict):
                body = {}
            devices_raw = body.get("devices", [])
            if not isinstance(devices_raw, list):
                devices_raw = []
            device_ids = [
                device_id for d in devices_raw if isinstance(d, dict) and (device_id := d.get("id"))
            ]
            states = await handle_devices_query(
                self._mass,
                device_ids,
                exposed_ids=self._exposed_ids,
                playlist_uris=self._playlist_uris,
            )
            return web.json_response(build_response(request_id, asdict(states)))
        except Exception:
            self._logger.exception("Error handling /user/devices/query")
            return web.json_response(build_response(request_id, {}), status=500)

    async def _handle_action(self, request: web.Request) -> web.Response:
        """Handle /user/devices/action — execute capability actions."""
        request_id = request.headers.get("X-Request-Id", "")
        if not self._validate_auth(request):
            return self._unauthorized_response(request_id)

        try:
            body = await request.json()
        except Exception:
            return web.json_response(build_response(request_id, {}), status=400)

        try:
            action_payload = parse_action_payload(body)
            result = await handle_devices_action(
                self._mass,
                action_payload,
                exposed_ids=self._exposed_ids,
                playlist_uris=self._playlist_uris,
            )
            return web.json_response(build_response(request_id, asdict(result)))
        except Exception:
            self._logger.exception("Error handling /user/devices/action")
            return web.json_response(build_response(request_id, {}), status=500)

    async def _handle_unlink(self, request: web.Request) -> web.Response:
        """Handle /user/unlink — user disconnected account."""
        request_id = request.headers.get("X-Request-Id", "")
        if not self._validate_auth(request):
            return self._unauthorized_response(request_id)

        try:
            result = await handle_user_unlink()
            return web.json_response(build_response(request_id, result))
        except Exception:
            self._logger.exception("Error handling /user/unlink")
            return web.json_response(build_response(request_id, {}), status=500)

    # -------------------------------------------------------------------
    # OAuth account linking
    # -------------------------------------------------------------------

    def _cleanup_expired_codes(self) -> None:
        """Remove expired authorization codes."""
        now = time.time()
        expired = [code for code, exp in self._pending_codes.items() if now > exp]
        for code in expired:
            del self._pending_codes[code]

    async def _handle_oauth_authorize(self, request: web.Request) -> web.Response:
        """
        Handle GET /auth/authorize — show authorization page.

        Yandex opens this URL in the user's browser during account linking.
        Parameters: client_id, redirect_uri, state, response_type=code
        """
        client_id = request.query.get("client_id", "")
        response_type = request.query.get("response_type", "")
        redirect_uri = request.query.get("redirect_uri", "")
        state = request.query.get("state", "")

        # Validate required OAuth parameters
        if client_id != DIRECT_OAUTH_CLIENT_ID:
            return web.Response(text="Invalid client_id", status=400)
        if response_type != "code":
            return web.Response(text="Invalid response_type", status=400)
        if not redirect_uri:
            return web.Response(text="Missing redirect_uri", status=400)

        # Validate redirect_uri is the expected Yandex HTTPS endpoint (prevent open redirect)
        parsed = urllib.parse.urlparse(redirect_uri)
        if parsed.scheme != "https" or parsed.hostname != "social.yandex.net":
            return web.Response(text="Invalid redirect_uri", status=400)

        # Generate authorization code (with DoS cap on pending codes)
        self._cleanup_expired_codes()
        if len(self._pending_codes) >= MAX_PENDING_CODES:
            return web.Response(text="Too many pending authorization requests", status=429)
        code = uuid.uuid4().hex
        self._pending_codes[code] = time.time() + OAUTH_CODE_EXPIRY

        # Build redirect URL with code and state
        params = {"code": code}
        if state:
            params["state"] = state
        separator = "&" if "?" in redirect_uri else "?"
        redirect_url = f"{redirect_uri}{separator}{urllib.parse.urlencode(params)}"

        html = _AUTHORIZE_HTML.format(redirect_url=html_module.escape(redirect_url, quote=True))
        return web.Response(text=html, content_type="text/html", status=200)

    async def _handle_oauth_token(self, request: web.Request) -> web.Response:
        """
        Handle POST /auth/token — exchange code for access token.

        Supports:
        - grant_type=authorization_code: exchange code for new token
        - grant_type=refresh_token: return same access token
        """
        try:
            data = await request.post()
        except Exception:
            return web.json_response({"error": "invalid_request"}, status=400)

        # Validate client credentials
        client_id = str(data.get("client_id", ""))
        client_secret = str(data.get("client_secret", ""))
        if client_id != DIRECT_OAUTH_CLIENT_ID or not secrets.compare_digest(
            client_secret, self._client_secret
        ):
            return web.json_response({"error": "invalid_client"}, status=401)

        grant_type = str(data.get("grant_type", ""))

        if grant_type == "authorization_code":
            code = str(data.get("code", ""))
            self._cleanup_expired_codes()

            if not code or code not in self._pending_codes:
                return web.json_response({"error": "invalid_grant"}, status=400)

            # Consume the code
            del self._pending_codes[code]

            # Generate or reuse access token
            if not self._access_token:
                self._access_token = uuid.uuid4().hex
                if self._on_token_created:
                    self._on_token_created(self._access_token)
                self._logger.info("Generated new access token for direct connection")

            return web.json_response(
                {
                    "access_token": self._access_token,
                    "token_type": "bearer",
                    "refresh_token": self._access_token,
                }
            )

        if grant_type == "refresh_token":
            refresh_token = str(data.get("refresh_token", ""))
            if refresh_token and secrets.compare_digest(refresh_token, self._access_token):
                return web.json_response(
                    {
                        "access_token": self._access_token,
                        "token_type": "bearer",
                        "refresh_token": self._access_token,
                    }
                )
            return web.json_response({"error": "invalid_grant"}, status=400)

        return web.json_response({"error": "unsupported_grant_type"}, status=400)
