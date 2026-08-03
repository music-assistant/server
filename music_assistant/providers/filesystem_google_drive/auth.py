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

from music_assistant.helpers.oauth import (
    OAUTH_STEP_TIMEOUT,
    authorization_code_from_params,
    hosted_bounce_redirect,
)
from music_assistant.models.setup_flow import SetupFlowError

from .constants import OAUTH_AUTHORIZE_URL, OAUTH_SCOPE, OAUTH_TOKEN_URL

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession


async def authorize(session: SetupSession, client_id: str, client_secret: str) -> str:
    """
    Run the Google OAuth consent flow via the setup session and return the refresh token.

    :param session: The setup session driving the flow.
    :param client_id: The user's Google OAuth client ID.
    :param client_secret: The user's Google OAuth client secret.
    """
    # Google only allows pre-registered redirect URIs, so send the user through the fixed
    # MA callback page which forwards to the local callback URL smuggled along in `state`
    redirect_uri, state = hosted_bounce_redirect(session.callback_url)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": OAUTH_SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
        # offline access + forced consent so Google always returns a refresh token
        "access_type": "offline",
        "prompt": "consent",
    }
    result = await session.external(
        f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}",
        step_id="authenticate",
        expires_in=OAUTH_STEP_TIMEOUT,
    )
    code = authorization_code_from_params(result)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    try:
        async with session.mass.http_session.post(OAUTH_TOKEN_URL, data=data) as resp:
            if resp.status != 200:
                raise SetupFlowError(f"Failed to exchange authorization code: {await resp.text()}")
            token_info = await resp.json()
    except ClientError as err:
        raise SetupFlowError(f"Failed to exchange authorization code: {err}") from err
    if not (refresh_token := token_info.get("refresh_token")):
        raise SetupFlowError(
            "Google did not return a refresh token, please retry the authorization"
        )
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
