"""Telmore Musik authentication manager."""

from __future__ import annotations

import re

from music_assistant_models.errors import LoginFailed
from yarl import URL

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.providers.music247e.auth_manager import (
    Music247eAccessToken,
    Music247eAuthManager,
)


class TelmoreAuthManager(Music247eAuthManager):
    """Telmore Musik authentication manager."""

    async def _fetch_token(self) -> Music247eAccessToken | None:
        """Perform the Telmore login flow and return a fresh access token."""
        # Try refresh token flow first
        if self._refresh_token:
            self.logger.debug("Trying to fetch refresh token")

            async with self.mass.http_session.post(
                "https://musik.telmore.dk/api/token",
                json={"refresh_token": self._refresh_token},
            ) as refresh_response:
                refresh_result = (
                    await refresh_response.json(content_type=None) if refresh_response.ok else {}
                )
                if refresh_result.get("status", 4) == 0:
                    access_token = refresh_result["tokenResult"]["access_token"]

                    self.logger.debug("Refresh token flow success")
                    self._refresh_token = refresh_result["tokenResult"]["refresh_token"]
                    return Music247eAccessToken(access_token)

            self.logger.warning(
                "Refresh token flow failed: status=%s", refresh_result.get("status")
            )
            # stale refresh token will keep failing, clear it so later logins skip it
            self._refresh_token = None

        async with self.mass.http_session.get(
            "https://musik.telmore.dk/api/delegatedlogin",
            allow_redirects=False,
        ) as delegate_response:
            session = URL(delegate_response.headers.get("Location", "")).query.get("session")
            if not session:
                raise LoginFailed("Telmore login failed: no session in delegated login response")

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
                raise LoginFailed(
                    f"Telmore login failed: internal-login returned HTTP {login_response.status}"
                )

            login_result = await login_response.json()
            login_url = login_result.get("url")
            if not login_url:
                raise LoginFailed("Telmore login failed: no redirect URL in login response")

        async with self.mass.http_session.get(login_url) as token_response:
            token_page = await token_response.text()
            access_token_re = re.search(r'accessToken:\s*"([^"]+)"', token_page)
            refresh_token_re = re.search(r'refreshToken:\s*"([^"]+)"', token_page)

            if not access_token_re or not refresh_token_re:
                raise LoginFailed(
                    "Telmore login failed: access/refresh token not found in response"
                )

            access_token = access_token_re.group(1)
            self._refresh_token = refresh_token_re.group(1)

            self.logger.debug("Got new auth token")

            return Music247eAccessToken(access_token)
