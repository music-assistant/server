"""
OAuth glue between Music Assistant and the Yandex Disk REST API.

Mirrors the Google Drive provider's token handling, using Yandex's
confirmation-code flow: the user registers their own Yandex OAuth application
(scope ``cloud_api:disk.read``), opens the authorize URL, and pastes the code
Yandex shows on its ``verification_code`` page. :func:`exchange_manual_code`
trades that code for a refresh token; :class:`MAYandexDiskAuth` then keeps a
valid access token, refreshing it with Yandex when it expires.

This flow needs no redirect URI to be registered (Yandex displays the code),
which is why it is the only flow offered.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from aiohttp import ClientError
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError

from .constants import (
    OAUTH_AUTHORIZE_URL,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
    VERIFICATION_CODE_REDIRECT,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


def manual_authorize_url(client_id: str) -> str:
    """
    Build the authorize URL the user opens to obtain a confirmation code.

    :param client_id: The user's Yandex OAuth client id.
    :returns: The URL that shows a confirmation code on Yandex's page.
    """
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": OAUTH_SCOPE,
        "force_confirm": "yes",
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_manual_code(
    mass: MusicAssistant, code: str, client_id: str, client_secret: str
) -> str:
    """
    Exchange a pasted confirmation code for a refresh token.

    :param mass: The MusicAssistant instance.
    :param code: The confirmation code copied from Yandex's verification page.
    :param client_id: The user's Yandex OAuth client id.
    :param client_secret: The user's Yandex OAuth client secret.
    :returns: The refresh token.
    :raises LoginFailed: The code is empty or the exchange failed.
    """
    if not code:
        raise LoginFailed("Enter the confirmation code from the Yandex page first")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": VERIFICATION_CODE_REDIRECT,
    }
    try:
        async with mass.http_session.post(OAUTH_TOKEN_URL, data=data) as resp:
            if resp.status != 200:
                raise LoginFailed(f"Failed to exchange authorization code: {await resp.text()}")
            token_info = await resp.json()
    except ClientError as err:
        raise LoginFailed(f"Failed to exchange authorization code: {err}") from err
    if not (refresh_token := token_info.get("refresh_token")):
        raise LoginFailed("Yandex did not return a refresh token, please retry the authorization")
    return str(refresh_token)


class MAYandexDiskAuth:
    """Provide Yandex Disk access tokens using a stored refresh token."""

    def __init__(
        self,
        mass: MusicAssistant,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        """
        Initialise the auth helper.

        :param mass: The MusicAssistant instance.
        :param client_id: The user's Yandex OAuth client id.
        :param client_secret: The user's Yandex OAuth client secret.
        :param refresh_token: The stored refresh token.
        """
        self.mass = mass
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def async_get_access_token(self) -> str:
        """
        Return a valid access token, refreshing it if needed.

        :returns: A currently-valid disk-scoped access token.
        :raises LoginFailed: No refresh token, or it was rejected.
        :raises ProviderUnavailableError: A transient failure reaching Yandex.
        """
        if not self._refresh_token:
            raise LoginFailed("Yandex Disk is not authorized; run the authorization first")
        # refresh 60s early so a token never expires mid-request
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        return await self._refresh()

    async def _refresh(self) -> str:
        """Exchange the refresh token for a new access token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            async with self.mass.http_session.post(OAUTH_TOKEN_URL, data=data) as resp:
                if resp.status in (400, 401):
                    # invalid_grant and friends: the refresh token was revoked
                    raise LoginFailed(f"Yandex token refresh failed: {await resp.text()}")
                resp.raise_for_status()
                payload = await resp.json()
        except ClientError as err:
            # 5xx or network blip: transient, so don't force the user to re-auth
            raise ProviderUnavailableError(f"Yandex token refresh failed: {err}") from err
        self._access_token = str(payload["access_token"])
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        # Yandex may rotate the refresh token; keep the newest
        if new_refresh := payload.get("refresh_token"):
            self._refresh_token = str(new_refresh)
        return self._access_token
