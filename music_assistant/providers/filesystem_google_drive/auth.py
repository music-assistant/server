"""
OAuth glue between Music Assistant and the Google Drive API library.

The google_drive_api library just needs one thing from us: a method that
returns a currently-valid access token. This class provides that, refreshing
the token with Google when it has expired.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from aiohttp import ClientError
from google_drive_api.auth import AbstractAuth
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError

from music_assistant.helpers.auth import AuthenticationHelper

from .constants import CALLBACK_REDIRECT_URL, OAUTH_AUTHORIZE_URL, OAUTH_SCOPE, OAUTH_TOKEN_URL

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def authorize(
    mass: MusicAssistant, session_id: str, client_id: str, client_secret: str
) -> str:
    """
    Run the Google OAuth consent flow and return the resulting refresh token.

    :param mass: MusicAssistant instance.
    :param session_id: Unique id for this auth session, provided by the frontend.
    :param client_id: The user's Google OAuth client ID.
    :param client_secret: The user's Google OAuth client secret.
    """
    async with AuthenticationHelper(mass, session_id) as auth_helper:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "scope": OAUTH_SCOPE,
            "redirect_uri": CALLBACK_REDIRECT_URL,
            # Google only allows pre-registered redirect URIs, so we send the user
            # through the fixed MA callback page which forwards to the local
            # (session-specific) callback URL we smuggle along in `state`
            "state": auth_helper.callback_url,
            # offline access + forced consent so Google always returns a refresh token
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"
        result = await auth_helper.authenticate(auth_url, timeout=120)
    # the callback relay page forwards a literal "null" code when consent was denied
    if not result.get("code") or result["code"] == "null":
        err = result.get("error", "no authorization code returned")
        raise LoginFailed(f"Google authorization failed: {err}")
    data = {
        "grant_type": "authorization_code",
        "code": result["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": CALLBACK_REDIRECT_URL,
    }
    try:
        async with mass.http_session.post(OAUTH_TOKEN_URL, data=data) as resp:
            if resp.status != 200:
                raise LoginFailed(f"Failed to exchange authorization code: {await resp.text()}")
            token_info = await resp.json()
    except ClientError as err:
        raise LoginFailed(f"Failed to exchange authorization code: {err}") from err
    if not (refresh_token := token_info.get("refresh_token")):
        raise LoginFailed("Google did not return a refresh token, please retry the authorization")
    return str(refresh_token)


class MAGoogleDriveAuth(AbstractAuth):
    """Provide Google Drive access tokens using a stored refresh token."""

    def __init__(
        self,
        mass: MusicAssistant,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        """Initialise the auth helper."""
        super().__init__(mass.http_session)
        self.mass = mass
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing it if needed."""
        # refresh 60s early so a token never expires mid-request
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        return await self._refresh()

    async def _refresh(self) -> str:
        """Exchange the refresh token for a new access token."""
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            async with self.mass.http_session.post(OAUTH_TOKEN_URL, data=data) as resp:
                if resp.status in (400, 401):
                    # invalid_grant and friends: the refresh token was revoked or expired
                    raise LoginFailed(f"Google token refresh failed: {await resp.text()}")
                resp.raise_for_status()
                payload = await resp.json()
        except ClientError as err:
            # 5xx or network blip: transient, so don't report it as an auth
            # problem that sends the user back through the OAuth flow
            raise ProviderUnavailableError(f"Google token refresh failed: {err}") from err
        self._access_token = str(payload["access_token"])
        self._expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._access_token
