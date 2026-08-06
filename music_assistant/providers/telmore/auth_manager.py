"""Telmore Musik authentication manager."""

import re
import time
from typing import TYPE_CHECKING

from yarl import URL

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.util import (
    lock,
    try_parse_int,
)
from music_assistant.providers.yousee.api_client import JsonLike

if TYPE_CHECKING:
    from music_assistant.providers.telmore.provider import TelmoreMusikProvider


class TelmoreAccessToken:
    """Telmore Musik access token wrapper."""

    def __init__(self, access_token: str) -> None:
        """Initialize TelmoreAccessToken."""
        self._access_token = access_token
        self._token_parts = self._parse_access_token(access_token)

    def is_expired(self) -> bool:
        """Return True if token is expired."""
        expires_at = try_parse_int(self._token_parts.get("ExpiresOn", 0))
        return not expires_at or expires_at <= time.time()

    def __str__(self) -> str:
        """Return string representation of the access token."""
        return self._access_token

    def _parse_access_token(self, token: str) -> JsonLike:
        return dict(part.split("=", 1) for part in token.split("&") if "=" in part)


class TelmoreAuthManager:
    """Telmore Musik authentication manager."""

    def __init__(self, provider: TelmoreMusikProvider):
        """Initialize TelmoreAuthManager."""
        self._access_token: TelmoreAccessToken | None = None
        self._refresh_token: str | None = None
        self.mass = provider.mass
        self.provider = provider
        self.logger = provider.logger

    def invalidate(self) -> None:
        """Invalidate current access token."""
        self._access_token = None

    @lock
    async def auth_token(self) -> TelmoreAccessToken | None:
        """Authenticate and return access token."""
        if self._access_token and not self._access_token.is_expired():
            return self._access_token

        # Try refresh token flow first
        if self._refresh_token:
            self.logger.debug("Trying to fetch refresh token")

            async with self.mass.http_session.post(
                "https://musik.telmore.dk/api/token",
                data={"refresh_token": self._refresh_token},
            ) as refresh_response:
                refresh_result = await refresh_response.json()
                if refresh_result.get("status", 4) == 0:
                    access_token = refresh_result["tokenResult"]["access_token"]

                    self.logger.debug("Refresh token flow success")
                    self._access_token = TelmoreAccessToken(access_token)
                    self._refresh_token = refresh_result["tokenResult"]["refresh_token"]
                    return self._access_token

        async with self.mass.http_session.get(
            "https://musik.telmore.dk/api/delegatedlogin",
            allow_redirects=False,
        ) as delegate_response:
            session = URL(delegate_response.headers.get("Location", "")).query.get("session")
            if not session:
                return None

        async with self.mass.http_session.post(
            "https://id.telmore.dk/internal-login",
            params={"session": session},
            json={
                "session": session,
                "username": self.provider.get_setup_value(CONF_USERNAME),
                "password": self.provider.get_setup_value(CONF_PASSWORD),
            },
        ) as login_response:
            if login_response.status != 200:
                return None

            login_result = await login_response.json()
            login_url = login_result.get("url")
            if not login_url:
                return None

        async with self.mass.http_session.get(login_url) as token_response:
            token_page = await token_response.text()
            access_token_re = re.search(r'accessToken:\s*"([^"]+)"', token_page)
            refresh_token_re = re.search(r'refreshToken:\s*"([^"]+)"', token_page)

            if not access_token_re or not refresh_token_re:
                return None

            access_token = access_token_re.group(1)
            self._refresh_token = refresh_token_re.group(1)

            self._access_token = TelmoreAccessToken(access_token)
            self.logger.debug("Got new auth token")

            return self._access_token
