"""YouSee Musik authentication manager."""

from __future__ import annotations

import re

from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.providers.music247e.auth_manager import (
    Music247eAccessToken,
    Music247eAuthManager,
)


class YouSeeAuthManager(Music247eAuthManager):
    """YouSee Musik authentication manager."""

    async def _fetch_token(self) -> Music247eAccessToken | None:
        """Perform the YouSee login flow and return a fresh access token."""
        # Try refresh token flow first
        if self._refresh_token:
            self.logger.debug("Trying to fetch refresh token")

            async with self.mass.http_session.post(
                "https://musik.yousee.dk/api/token", data={"refresh_token": self._refresh_token}
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
            "https://musik.yousee.dk/api/delegatedlogin"
        ) as delegate_response:
            post_action_re = re.search('action="([^"]+)"', await delegate_response.text())
            if not post_action_re:
                raise LoginFailed("YouSee login failed: no login form in delegated login response")

            cookies = delegate_response.cookies

            async with self.mass.http_session.post(
                f"https://login.yousee.dk{post_action_re.group(1)}",
                data={
                    "pf.username": self.provider.get_setup_value(CONF_USERNAME),
                    "pf.pass": self.provider.get_setup_value(CONF_PASSWORD),
                    "pf.ok": "clicked",
                    "pf.adapterId": "MusicUsernamePasswordAdapter",
                },
                cookies=cookies,
            ) as login_response:
                login_page = await login_response.text()
                access_token_re = re.search(
                    r'localStorage.setItem\("accesstoken", "([^"]+)"', login_page
                )
                refresh_token_re = re.search(
                    r'localStorage.setItem\("refreshtoken", "([^"]+)"', login_page
                )

                if not access_token_re or not refresh_token_re:
                    raise LoginFailed(
                        "YouSee login failed: access/refresh token not found in response"
                    )

                access_token = access_token_re.group(1)
                self._refresh_token = refresh_token_re.group(1)

                self.logger.debug("Got new auth token")

                return Music247eAccessToken(access_token)
