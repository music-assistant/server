"""VRT MAX authentication: SSO login and player-token exchange."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import jwt

from music_assistant.helpers.aiohttp_client import create_clientsession

from .constants import (
    _TOKEN_EXPIRY_MARGIN,
    GRAPHQL_TIMEOUT,
    SSO_INIT_URL,
    SSO_LOGIN_URL,
    TOKEN_URL,
)
from .models import VrtAuthError

if TYPE_CHECKING:
    import logging

    from aiohttp import ClientSession

    from music_assistant.mass import MusicAssistant


class VrtMaxAuth:
    """
    Manages VRT MAX authentication for on-demand playback.

    Performs the SSO username/password login to obtain an identity token, then
    exchanges it for a short-lived vrtPlayerToken which it caches until shortly
    before expiry. A single lock serialises concurrent refreshes.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        session: ClientSession,
        logger: logging.Logger,
        username: str,
        password: str,
    ) -> None:
        """
        Initialize the auth manager.

        :param mass: The MusicAssistant instance (used to build the login session).
        :param logger: Logger for diagnostics.
        :param session: Shared aiohttp session (used for the token exchange).
        :param username: VRT account email (empty disables on-demand).
        :param password: VRT account password (empty disables on-demand).
        """
        self._mass = mass
        self._session = session
        self._logger = logger
        self._username = username
        self._password = password
        self._lock = asyncio.Lock()
        self._access_token: str | None = None
        self._identity_token: str | None = None
        self._login_expiry: float = 0.0
        self._player_token: str | None = None
        self._player_token_expiry: float = 0.0

    @property
    def enabled(self) -> bool:
        """Return True when credentials are configured."""
        return bool(self._username and self._password)

    async def get_access_token(self) -> str:
        """Return a valid access token (Bearer) for user-scoped GraphQL calls."""
        if not self.enabled:
            raise VrtAuthError("VRT account credentials are required")
        async with self._lock:
            await self._ensure_login()
            assert self._access_token is not None
            return self._access_token

    async def get_player_token(self) -> str:
        """Return a valid vrtPlayerToken, logging in / refreshing as needed."""
        if not self.enabled:
            raise VrtAuthError("VRT account credentials are required for on-demand playback")
        async with self._lock:
            if (
                self._player_token
                and time.time() < self._player_token_expiry - _TOKEN_EXPIRY_MARGIN
            ):
                return self._player_token
            await self._ensure_login()
            assert self._identity_token is not None
            token, expiry = await self._request_player_token(self._identity_token)
            self._player_token = token
            self._player_token_expiry = expiry
            return token

    async def _ensure_login(self) -> None:
        """Ensure a valid access + identity token, performing the SSO login if needed."""
        if (
            self._access_token
            and self._identity_token
            and time.time() < self._login_expiry - _TOKEN_EXPIRY_MARGIN
        ):
            return
        jar = aiohttp.CookieJar()
        try:
            async with create_clientsession(self._mass, cookie_jar=jar) as session:
                async with session.get(SSO_INIT_URL, timeout=GRAPHQL_TIMEOUT) as resp:
                    await resp.read()
                xsrf = _cookie_value(jar, "OIDCXSRF")
                if not xsrf:
                    raise VrtAuthError("VRT SSO init failed (no OIDCXSRF cookie)")
                payload = {
                    "clientId": "vrtnu-site",
                    "loginID": self._username,
                    "password": self._password,
                }
                async with session.post(
                    SSO_LOGIN_URL,
                    json=payload,
                    headers={"OIDCXSRF": xsrf},
                    timeout=GRAPHQL_TIMEOUT,
                ) as resp:
                    info = await resp.json(content_type=None)
                if not isinstance(info, dict) or info.get("errorCode") != 0:
                    message = (info or {}).get("errorMessage") or "invalid credentials"
                    raise VrtAuthError(f"VRT login failed: {message}")
                redirect_url = info.get("redirectUrl")
                if not redirect_url:
                    raise VrtAuthError("VRT login returned no redirect url")
                async with session.get(redirect_url, timeout=GRAPHQL_TIMEOUT) as resp:
                    await resp.read()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise VrtAuthError(f"VRT login request failed: {err}") from err
        access_token = _cookie_value(jar, "vrtnu-site_profile_at")
        identity_token = _cookie_value(jar, "vrtnu-site_profile_vt")
        if not access_token or not identity_token:
            raise VrtAuthError("VRT login did not yield the expected tokens")
        self._access_token = access_token
        self._identity_token = identity_token
        self._login_expiry = min(_jwt_expiry(access_token), _jwt_expiry(identity_token))

    async def _request_player_token(self, identity_token: str) -> tuple[str, float]:
        """Exchange an identity token for a vrtPlayerToken and its expiry epoch."""
        payload = {"identityToken": identity_token, "playerInfo": ""}
        try:
            async with self._session.post(
                TOKEN_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=GRAPHQL_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                body = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise VrtAuthError(f"Player token request failed: {err}") from err
        token = body.get("vrtPlayerToken") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise VrtAuthError("No vrtPlayerToken in token response")
        return token, _jwt_expiry(token)


def _cookie_value(jar: aiohttp.CookieJar, name: str) -> str | None:
    """Return the value of a cookie by name from a cookie jar."""
    for cookie in jar:
        if cookie.key == name and cookie.value:
            return cookie.value
    return None


def _jwt_expiry(token: str) -> float:
    """
    Return the `exp` claim (epoch seconds) of a VRT token.

    :param token: The JWT to read the expiry from.
    """
    # The signature is VRT's to verify, not ours; we only need the expiry so we know
    # when to refresh. A token we cannot read the expiry from is unusable, so fail
    # rather than guess at a lifetime.
    try:
        claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as err:
        raise VrtAuthError(f"Could not read the expiry from a VRT token: {err}") from err
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        raise VrtAuthError("VRT token has no usable 'exp' claim")
    return float(exp)
