"""YouSee Musik authentication manager."""

import re
import time
from typing import TYPE_CHECKING

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.util import (
    lock,
    try_parse_int,
)
from music_assistant.providers.yousee.api_client import JsonLike

if TYPE_CHECKING:
    from music_assistant.providers.yousee.provider import YouSeeMusikProvider


class YouSeeAccessToken:
    """YouSee Musik access token wrapper."""

    def __init__(self, access_token: str) -> None:
        """Initialize YouSeeAccessToken."""
        self._access_token = access_token
        self._token_parts = self._parse_access_token(access_token)

    def is_expired(self) -> bool:
        """Return True if token is expired."""
        expires_at = try_parse_int(self._token_parts.get("ExpiresOn", 0))
        return not expires_at or expires_at <= time.time()

    def _parse_access_token(self, token: str) -> JsonLike:
        return dict(part.split("=", 1) for part in token.split("&") if "=" in part)

    def __str__(self) -> str:
        """Return string representation of the access token."""
        return self._access_token


class YouSeeAuthManager:
    """YouSee Musik authentication manager."""

    def __init__(self, provider: "YouSeeMusikProvider"):
        """Initialize YouSeeAuthManager."""
        self._access_token: YouSeeAccessToken | None = None
        self._refresh_token: str | None = None
        self.mass = provider.mass
        self.provider = provider
        self.logger = provider.logger

    def invalidate(self) -> None:
        """Invalidate current access token."""
        self._access_token = None

    @lock
    async def auth_token(self) -> YouSeeAccessToken | None:
        """Authenticate and return access token."""
        if self._access_token and not self._access_token.is_expired():
            return self._access_token

        # Try refresh token flow first
        if self._refresh_token:
            self.logger.debug("Trying to fetch refresh token")

            async with self.mass.http_session.post(
                "https://musik.yousee.dk/api/token", data={"refresh_token": self._refresh_token}
            ) as refresh_response:
                refresh_result = await refresh_response.json()
                if refresh_result.get("status", 4) == 0:
                    access_token = refresh_result["tokenResult"]["access_token"]

                    self.logger.debug("Refresh token flow success")
                    self._access_token = YouSeeAccessToken(access_token)
                    self._refresh_token = refresh_result["tokenResult"]["refresh_token"]
                    return self._access_token

        async with (
            self.mass.http_session.get(
                "https://musik.yousee.dk/api/delegatedlogin"
            ) as delegate_response,
        ):
            post_action_re = re.search('action="([^"]+)"', await delegate_response.text())
            if not post_action_re:
                return None

            cookies = delegate_response.cookies

            async with self.mass.http_session.post(
                f"https://login.yousee.dk{post_action_re.group(1)}",
                data={
                    "pf.username": self.provider.config.get_value(CONF_USERNAME),
                    "pf.pass": self.provider.config.get_value(CONF_PASSWORD),
                    "pf.ok": "clicked",
                    "pf.adapterId": "MusicUsernamePasswordAdapter",
                },
                cookies=cookies,
            ) as login_response:
                access_token_re = re.search(
                    r'localStorage.setItem\("accesstoken", "([^"]+)"',
                    await login_response.text(),
                )

                refresh_token_re = re.search(
                    r'localStorage.setItem\("refreshtoken", "([^"]+)"',
                    await login_response.text(),
                )

                if not access_token_re or not refresh_token_re:
                    return None

                access_token = access_token_re.group(1)
                self._refresh_token = refresh_token_re.group(1)

                self._access_token = YouSeeAccessToken(access_token)
                self.logger.debug("Got new auth token")

                return self._access_token
