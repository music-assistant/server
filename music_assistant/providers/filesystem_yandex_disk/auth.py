"""OAuth token maintenance for the Yandex Disk REST API."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant_models.errors import LoginFailed
from ya_passport_auth.ma import refresh_oauth_tokens

from .constants import OAUTH_SCOPE

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant import MusicAssistant


class MAYandexDiskAuth:
    """Provide Yandex Disk access tokens using a stored refresh token."""

    def __init__(
        self,
        mass: MusicAssistant,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        persist_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        """
        Initialise the auth helper.

        :param mass: The MusicAssistant instance.
        :param client_id: The user's Yandex OAuth client id.
        :param client_secret: The user's Yandex OAuth client secret.
        :param refresh_token: The stored refresh token.
        :param persist_refresh_token: Callback that stores a rotated token.
        """
        self.mass = mass
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._persist_refresh_token = persist_refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def async_get_access_token(self) -> str:
        """
        Return a valid access token, refreshing it if needed.

        :returns: A currently-valid disk-scoped access token.
        :raises LoginFailed: No refresh token, or it was rejected.
        :raises ResourceTemporarilyUnavailable: A transient failure reaching Yandex.
        """
        if not self._refresh_token:
            raise LoginFailed("Yandex Disk is not authorized; run the authorization first")
        # refresh 60s early so a token never expires mid-request
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        return await self._refresh()

    async def _refresh(self) -> str:
        """Exchange the refresh token for a new access token."""
        tokens = await refresh_oauth_tokens(
            client_id=self._client_id,
            client_secret=self._client_secret,
            refresh_token=self._refresh_token,
            scope=OAUTH_SCOPE,
            session=self.mass.http_session,
        )
        self._access_token = tokens.access_token.get_secret()
        self._expires_at = time.time() + tokens.expires_in
        refresh_token = tokens.refresh_token.get_secret()
        if refresh_token != self._refresh_token:
            self._refresh_token = refresh_token
            if self._persist_refresh_token is not None:
                self._persist_refresh_token(refresh_token)
        return self._access_token
