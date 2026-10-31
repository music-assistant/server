"""Live Phish music provider support for Music Assistant."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from time import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlparse

from aiohttp import ClientTimeout, ContentTypeError
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.aiohttp_client import create_clientsession
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.livephish.constants import (
    ACCOUNT_URL,
    AUTHORIZE_URL,
    CLIENT_ID,
    GRANT_TYPE,
    REDIRECT_URI,
    SCOPE,
    TOKEN_URL,
)

if TYPE_CHECKING:
    from aiohttp import ClientResponse, ClientSession
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_VERIFICATION_TOKEN = re.compile(
    r"""name=["']__RequestVerificationToken["'][^>]*value=["']([^"']+)["']"""
    r"""|value=["']([^"']+)["'][^>]*name=["']__RequestVerificationToken["']""",
    re.IGNORECASE,
)
_MAX_LOGIN_REDIRECTS = 8

# Catalog features land once a real account response confirms the payload shapes.
SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LivePhishProvider(mass, manifest, config, SUPPORTED_FEATURES)


class LivePhishProvider(MusicProvider):
    """Provider implementation for Live Phish."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature] | None = None,
    ) -> None:
        """Initialize the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._auth_token: str | None = None
        self._token_expiry = 0.0

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # a reload may follow a switch to another account, which cached details cannot tell
        await self.mass.cache.delete(self._get_account.__name__, provider=self.instance_id)
        await self.login()

    async def login(self) -> str:
        """Login to Live Phish and return the bearer token."""
        if self._auth_token and self._token_expiry > time():
            return self._auth_token
        username = self.get_setup_value(CONF_USERNAME)
        password = self.get_setup_value(CONF_PASSWORD)
        if not username or not password:
            msg = "Invalid login credentials"
            raise LoginFailed(msg)
        # This public client rejects grant_type=password with unauthorized_client.
        # Sign in through the identity server's login form, then exchange the code.
        code, verifier = await self._authorization_code(str(username), str(password))
        token = await self._exchange_code(code, verifier)
        access_token = token["access_token"]
        expires_in = token["expires_in"]
        self._auth_token = access_token
        self._token_expiry = time() + expires_in
        # Drop the token again unless the account endpoint accepts it. A later call
        # would otherwise reuse a bearer the API already rejected.
        accepted = False
        try:
            await self._get_account()
            accepted = True
        finally:
            if not accepted:
                self._auth_token = None
                self._token_expiry = 0
        return access_token

    async def _authorization_code(self, username: str, password: str) -> tuple[str, str]:
        """Sign in on the identity server and return the authorization code and PKCE verifier."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
        challenge = challenge.rstrip("=")
        # A fresh cookie jar so a previous identity session cannot replace these credentials.
        session = create_clientsession(self.mass)
        try:
            timeout = ClientTimeout(total=120)
            async with session.get(
                AUTHORIZE_URL,
                params={
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "response_type": "code",
                    "scope": SCOPE,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
                allow_redirects=True,
                timeout=timeout,
            ) as response:
                html = await response.text()
                login_url = str(response.url)
            verification = _verification_token(html)
            if verification is None:
                raise LoginFailed("Live Phish login page could not be read")
            async with session.post(
                login_url,
                data={
                    "Input.Email": username,
                    "Input.Password": password,
                    "Input.RememberLogin": "false",
                    "__RequestVerificationToken": verification,
                },
                allow_redirects=False,
                timeout=timeout,
            ) as response:
                code = await self._code_from_login(session, response)
            return code, verifier
        finally:
            await session.close()

    async def _code_from_login(self, session: ClientSession, response: ClientResponse) -> str:
        """Follow the login response until the player redirect carries an authorization code."""
        status = response.status
        location = response.headers.get("Location")
        current_url = str(response.url)
        body = "" if status in _REDIRECTS else await response.text()
        for _ in range(_MAX_LOGIN_REDIRECTS):
            if status in _REDIRECTS and location:
                next_url = urljoin(current_url, location)
                if code := _code_from_redirect(next_url):
                    return code
                timeout = ClientTimeout(total=120)
                async with session.get(next_url, allow_redirects=False, timeout=timeout) as hop:
                    status = hop.status
                    location = hop.headers.get("Location")
                    current_url = str(hop.url)
                    body = "" if status in _REDIRECTS else await hop.text()
                continue
            if "Invalid username or password" in body:
                raise LoginFailed("Invalid Live Phish username or password")
            if "valid e-mail" in body.lower():
                raise LoginFailed("Live Phish username must be the account email address")
            break
        raise LoginFailed("Live Phish login did not return an authorization code")

    async def _exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        """Exchange an authorization code for a token response."""
        timeout = ClientTimeout(total=120)
        async with self.mass.http_session.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": GRANT_TYPE,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
            ssl=True,
            timeout=timeout,
        ) as response:
            if response.status in (502, 503):
                raise ResourceTemporarilyUnavailable(backoff_time=30)
            if response.status >= 400:
                raise LoginFailed(await _token_error(response))
            return await response.json()

    async def _get_account(self) -> Any:
        """Return the authenticated account payload from the subscriptions endpoint."""
        headers = {"Authorization": f"Bearer {self._auth_token}"}
        timeout = ClientTimeout(total=120)
        async with self.mass.http_session.get(
            ACCOUNT_URL, headers=headers, ssl=True, timeout=timeout
        ) as response:
            if response.status == 401:
                raise LoginFailed("Live Phish authentication expired or was rejected")
            response.raise_for_status()
            return await response.json()


def _verification_token(html: str) -> str | None:
    """Return the antiforgery token from the identity login page."""
    match = _VERIFICATION_TOKEN.search(html)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _code_from_redirect(url: str) -> str | None:
    """Return the authorization code when the URL is the player's redirect."""
    parsed = urlparse(url)
    expected = urlparse(REDIRECT_URI)
    if (parsed.scheme, parsed.netloc, parsed.path) != (
        expected.scheme,
        expected.netloc,
        expected.path,
    ):
        return None
    codes = parse_qs(parsed.query).get("code") or []
    return codes[0] if codes and codes[0] else None


async def _token_error(response: ClientResponse) -> str:
    """Return a login failure message from a rejected token response."""
    try:
        payload = await response.json(content_type=None)
    except ContentTypeError, ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if error:
        return f"Live Phish login failed ({error})"
    return f"Live Phish login failed (HTTP {response.status})"
