"""Telmore Musik authentication manager."""

from __future__ import annotations

import re

from yarl import URL

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.util import lock
from music_assistant.providers.music247e.auth_manager import (
    Music247eAccessToken,
    Music247eAuthManager,
)


class TelmoreAuthManager(Music247eAuthManager):
    """Telmore Musik authentication manager."""

    @lock
    async def auth_token(self) -> Music247eAccessToken | None:
        """Authenticate and return access token."""
        if self._access_token and not self._access_token.is_expired():
            return self._access_token

        # Try refresh token flow first
        if self._refresh_token:
            self.logger.debug("Trying to fetch refresh token")

            async with self.mass.http_session.post(
                "https://musik.telmore.dk/api/token",
                json={"refresh_token": self._refresh_token},
            ) as refresh_response:
                refresh_result = await refresh_response.json()
                if refresh_result.get("status", 4) == 0:
                    access_token = refresh_result["tokenResult"]["access_token"]

                    self.logger.debug("Refresh token flow success")
                    self._access_token = Music247eAccessToken(access_token)
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

            self._access_token = Music247eAccessToken(access_token)
            self.logger.debug("Got new auth token")

            return self._access_token
