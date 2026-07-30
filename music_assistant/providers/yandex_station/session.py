"""
Yandex Session — HTTP client with cookie/CSRF management.

Adapted from AlexxIT/YandexStation (MIT license).
Authentication flows delegate to ``ya-passport-auth`` via an injected
``PassportClient``; this module handles HTTP requests with automatic
cookie refresh and CSRF token management.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from ya_passport_auth.exceptions import YaPassportError

if TYPE_CHECKING:
    from aiohttp import ClientResponse, ClientSession
    from ya_passport_auth import PassportClient, SecretStr

from .constants import API_REQUEST_INTERVAL

_LOGGER = logging.getLogger(__name__)


class YandexSession:
    """
    Yandex HTTP client with cookie/CSRF management.

    Manages x_token (long-lived ~1 year), music_token (for Glagol API),
    cookies, and CSRF tokens for Quasar API.  Auth operations delegate to
    the injected ``PassportClient`` which shares the same aiohttp session
    and cookie jar.
    """

    def __init__(
        self,
        session: ClientSession,
        client: PassportClient,
        x_token: SecretStr | None = None,
        music_token: SecretStr | None = None,
        refresh_token: SecretStr | None = None,
    ) -> None:
        """Initialize with aiohttp session, PassportClient, and optional credentials."""
        self._session = session
        self._client = client
        self.x_token = x_token
        self.music_token = music_token
        self.refresh_token = refresh_token
        self.csrf_token: str | None = None
        self.last_ts: float = 0

    # ── Token management ─────────────────────────────────────────

    async def get_music_token(self) -> SecretStr:
        """Get music token using x-token (for Glagol API auth)."""
        if not self.x_token:
            msg = "No x_token available to refresh music token"
            raise RuntimeError(msg)
        _LOGGER.debug("Requesting music token")
        return await self._client.refresh_music_token(self.x_token)

    async def login_token(self) -> bool:
        """Login to Yandex with x-token to obtain session cookies."""
        if not self.x_token:
            return False
        _LOGGER.debug("Login with x-token")
        try:
            await self._client.refresh_passport_cookies(self.x_token)
            return True
        except YaPassportError:
            _LOGGER.exception("Login with token failed")
            return False

    async def refresh_cookies(self) -> bool:
        """
        Check cookies and refresh if needed.

        Yandex may answer with an HTML error/redirect page when cookies are
        stale; awaiting ``r.json()`` on that would raise and break the
        ``_request()`` 401 retry/reauth flow.  Treat any non-200 response or
        non-JSON body as "cookies invalid" and fall back to ``login_token()``.
        """
        async with self._session.get("https://yandex.ru/quasar?storage=1") as r:
            if r.status != 200:
                return await self.login_token()
            try:
                resp = await r.json(content_type=None)
            except aiohttp.ContentTypeError, ValueError:
                return await self.login_token()
            if resp.get("storage", {}).get("user", {}).get("uid"):
                return True

        return await self.login_token()

    async def ensure_music_token(self) -> None:
        """Ensure music_token is available, fetching it if needed."""
        if not self.music_token and self.x_token:
            self.music_token = await self.get_music_token()

    # ── HTTP methods ─────────────────────────────────────────────

    async def get(self, url: str, **kwargs: Any) -> ClientResponse:
        """GET request with automatic auth for Glagol/Music API."""
        if url.startswith(("https://quasar.yandex.net/glagol/", "https://api.music.yandex.net/")):
            return await self._request_glagol(url, **kwargs)
        return await self._request("get", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> ClientResponse:
        """POST request with CSRF token management."""
        return await self._request("post", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> ClientResponse:
        """PUT request with CSRF token management."""
        return await self._request("put", url, **kwargs)

    async def ws_connect(self, url: str, **kwargs: Any) -> Any:
        """Create a WebSocket connection."""
        return await self._session.ws_connect(url, **kwargs)

    async def _request(
        self, method: str, url: str, retry: int = 2, **kwargs: Any
    ) -> ClientResponse:
        """Request with CSRF token and retry logic."""
        # DDoS protection
        while (delay := self.last_ts + API_REQUEST_INTERVAL - time.time()) > 0:
            await asyncio.sleep(delay)
        self.last_ts = time.time()

        # Non-GET requests need CSRF token for Quasar API
        if method != "get" and not url.startswith("https://rpc.alice.yandex.ru"):
            if self.csrf_token is None:
                _LOGGER.debug("Refreshing CSRF token")
                try:
                    csrf_secret = await self._client.get_quasar_csrf_token()
                    self.csrf_token = csrf_secret.get_secret()
                except YaPassportError as err:
                    msg = "Failed to obtain CSRF token"
                    raise RuntimeError(msg) from err
            kwargs.setdefault("headers", {})["x-csrf-token"] = self.csrf_token

        r: ClientResponse = await getattr(self._session, method)(url, **kwargs)
        if r.status == 200:
            return r

        # Release the failed response to avoid connection leaks
        try:
            await r.read()
        finally:
            r.release()

        if r.status == 400:
            retry = 0
        elif r.status == 401:
            await self.refresh_cookies()
        elif r.status == 403:
            self.csrf_token = None

        if retry:
            _LOGGER.debug("Retry %s %s", method, url)
            return await self._request(method, url, retry - 1, **kwargs)

        msg = f"{url} returned {r.status}"
        raise RuntimeError(msg)

    async def _request_glagol(self, url: str, retry: int = 2, **kwargs: Any) -> ClientResponse:
        """Request to Glagol/Music API with music_token auth."""
        await self.ensure_music_token()

        headers = kwargs.pop("headers", {})
        if self.music_token:
            headers["Authorization"] = f"OAuth {self.music_token.get_secret()}"
        r: ClientResponse = await self._session.get(url, headers=headers, **kwargs)
        if r.status == 200:
            return r

        # Release the failed response to avoid connection leaks
        try:
            await r.read()
        finally:
            r.release()

        if r.status == 403:
            self.music_token = None

        if retry:
            _LOGGER.debug("Retry Glagol request %s", url)
            return await self._request_glagol(url, retry - 1, **kwargs)

        msg = f"{url} returned HTTP {r.status} ({r.reason or 'no reason'})"
        raise RuntimeError(msg)
