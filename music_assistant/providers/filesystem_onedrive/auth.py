"""
OAuth glue between Music Assistant and OneDrive (Microsoft Graph).

The OneDrive SDK just needs a coroutine that returns a valid access token;
MAOneDriveAuth provides that, refreshing with Microsoft when it expires.
`authorize()` runs the one-time consent flow and returns a refresh token.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from aiohttp import ClientError
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.helpers.oauth import authorization_code_from_params, hosted_bounce_redirect
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.filesystem_cloud.base import CONF_REFRESH_TOKEN

from .constants import OAUTH_AUTHORIZE_URL, OAUTH_SCOPE, OAUTH_TOKEN_URL

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession


async def authorize(session: SetupSession, client_id: str, client_secret: str) -> str:
    """
    Run the Microsoft OAuth consent flow via the setup session and return the refresh token.

    :param session: The setup session driving the flow.
    :param client_id: The user's Microsoft OAuth client (application) ID.
    :param client_secret: The user's Microsoft OAuth client secret.
    """
    # Microsoft only allows pre-registered redirect URIs, so send the user through the fixed
    # MA callback page which forwards to the local callback URL smuggled along in `state`
    redirect_uri, state = hosted_bounce_redirect(session.callback_url)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": OAUTH_SCOPE,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    result = await session.external(
        f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}", step_id="authenticate"
    )
    code = authorization_code_from_params(result)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "scope": OAUTH_SCOPE,
    }
    try:
        async with session.mass.http_session.post(OAUTH_TOKEN_URL, data=data) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise SetupFlowError(
                    _friendly_auth_error(error_text)
                    or f"Failed to exchange authorization code: {error_text}"
                )
            token_info = await resp.json()
    except ClientError as err:
        raise SetupFlowError(f"Failed to exchange authorization code: {err}") from err
    if not (refresh_token := token_info.get("refresh_token")):
        raise SetupFlowError(
            "Microsoft did not return a refresh token, please retry the authorization"
        )
    return str(refresh_token)


class MAOneDriveAuth:
    """Provide OneDrive access tokens using a stored refresh token."""

    def __init__(
        self,
        mass: MusicAssistant,
        instance_id: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        """Initialise the auth helper."""
        self.mass = mass
        self._instance_id = instance_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        # Microsoft rotates the refresh token on every redemption, so parallel
        # refreshes with the same token could invalidate it; serialize them
        self._refresh_lock = asyncio.Lock()

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing it if needed."""
        # refresh 60s early so a token never expires mid-request
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        async with self._refresh_lock:
            # another waiter may have refreshed while we were queued
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
            "scope": OAUTH_SCOPE,
        }
        try:
            async with self.mass.http_session.post(OAUTH_TOKEN_URL, data=data) as resp:
                if resp.status in (400, 401):
                    error_text = await resp.text()
                    raise LoginFailed(
                        _friendly_auth_error(error_text)
                        or f"Microsoft token refresh failed: {error_text}"
                    )
                resp.raise_for_status()
                payload = await resp.json()
        except ClientError as err:
            # network trouble or a Microsoft outage, not an auth problem
            raise ProviderUnavailableError(f"Microsoft token refresh failed: {err}") from err
        self._access_token = str(payload["access_token"])
        self._expires_at = time.time() + float(payload["expires_in"])
        # Microsoft rotates the refresh token on every use and the old one only
        # stays valid for a limited time, so persist the newest to setup_data to
        # survive restarts
        if (new_refresh := payload.get("refresh_token")) and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            self.mass.config.set(
                f"{CONF_PROVIDERS}/{self._instance_id}/setup_data/{CONF_REFRESH_TOKEN}",
                self.mass.config.encrypt_string(new_refresh),
                immediate=True,
            )
        return self._access_token


def _friendly_auth_error(error_text: str) -> str | None:
    """Map well-known Microsoft auth errors to an actionable message, if recognized."""
    if "AADSTS7000222" in error_text:
        return (
            "The Microsoft OAuth client secret has expired. Create a new client secret "
            "in the Azure portal, update it in the provider settings and re-authorize."
        )
    if "AADSTS7000215" in error_text:
        return (
            "The Microsoft OAuth client secret is invalid. Make sure you copied the "
            "secret's Value (not the Secret ID) from the Azure portal."
        )
    if "invalid_grant" in error_text:
        return "The OneDrive authorization has expired or was revoked, please re-authorize."
    return None
