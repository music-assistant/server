"""Authentication manager for Tidal integration."""

import asyncio
import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.errors import LoginFailed

from music_assistant.helpers.app_vars import app_var

from .constants import AUTH_SCOPE, AUTH_URL, SESSIONS_URL

if TYPE_CHECKING:
    from aiohttp import ClientResponse, ClientSession

TOKEN_REFRESH_BUFFER = 60 * 7  # 7 minutes
# Minimum time between two token refreshes, so that (concurrent) requests
# hitting 401s cannot hammer the token endpoint with refresh calls.
TOKEN_REFRESH_COOLDOWN = 30


def _v2_client_credentials() -> tuple[str, str]:
    """Return the (client_id, client_secret) of the Tidal v2 (device) client."""
    return app_var("tidal_client_id_v2"), app_var("tidal_client_secret_v2")


async def _read_json(response: ClientResponse, context: str) -> dict[str, Any]:
    """
    Parse an auth response body as JSON, raising LoginFailed on a non-JSON body.

    :param response: The auth endpoint response to parse.
    :param context: Short description of the request, used in the error message.
    """
    # A proxy/gateway error can carry an HTML body; content_type=None skips
    # aiohttp's content-type check so such a failure surfaces as LoginFailed
    # instead of a ContentTypeError escaping the login flow's error contract.
    try:
        return cast("dict[str, Any]", await response.json(content_type=None))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise LoginFailed(f"{context} failed: HTTP {response.status} (non-JSON response)") from err


def _basic_auth_headers(client_id: str, client_secret: str) -> dict[str, str]:
    """Build the HTTP Basic auth + form headers for a token endpoint request."""
    token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }


@dataclass
class TidalUser:
    """Represent a Tidal user with their associated account information."""

    user_id: str | None = None
    country_code: str | None = None
    session_id: str | None = None
    profile_name: str | None = None
    user_name: str | None = None
    email: str | None = None


class TidalAuthManager:
    """Manager for Tidal authentication process."""

    def __init__(
        self,
        http_session: ClientSession,
        config_updater: Callable[[dict[str, Any]], None],
        logger: Any,
    ):
        """Initialize Tidal auth manager."""
        self.http_session = http_session
        self.update_config = config_updater
        self.logger = logger
        self._auth_info: dict[str, Any] | None = None
        self._refresh_lock = asyncio.Lock()
        self._last_refresh: float = 0.0
        self.user = TidalUser()

    async def initialize(self, auth_data: str) -> bool:
        """Initialize the auth manager with stored auth data."""
        if not auth_data:
            return False

        # Parse stored auth data
        try:
            self._auth_info = json.loads(auth_data)
        except json.JSONDecodeError as err:
            self.logger.error("Invalid authentication data: %s", err)
            return False

        # Ensure we have a valid token
        return await self.ensure_valid_token()

    @property
    def user_id(self) -> str | None:
        """Return the current user ID."""
        return self.user.user_id

    @property
    def country_code(self) -> str | None:
        """Return the current country code."""
        return self.user.country_code

    @property
    def session_id(self) -> str | None:
        """Return the current session ID."""
        return self.user.session_id

    @property
    def access_token(self) -> str | None:
        """Return the current access token."""
        return self._auth_info.get("access_token") if self._auth_info else None

    async def ensure_valid_token(self) -> bool:
        """Ensure we have a valid token, refresh if needed."""
        if not self._auth_info:
            return False

        # Check if token is expired
        expires_at = self._auth_info.get("expires_at", 0)
        if expires_at > time.time() + TOKEN_REFRESH_BUFFER:
            return True

        # Need to refresh token
        return await self.refresh_token()

    async def refresh_token(self) -> bool:
        """Refresh the auth token (single-flight, with a short cooldown)."""
        async with self._refresh_lock:
            # A refresh that just completed (e.g. by a concurrent request that
            # hit the same 401) is considered good: don't hit the token
            # endpoint again, let the caller retry with the current token.
            if time.time() - self._last_refresh < TOKEN_REFRESH_COOLDOWN:
                return True
            if not await self._perform_refresh():
                return False
            self._last_refresh = time.time()
            return True

    async def update_user_info(self, user_info: dict[str, Any], session_id: str) -> None:
        """Update user info from API response."""
        # Update the TidalUser dataclass with values from API response
        self.user = TidalUser(
            user_id=user_info.get("id"),
            country_code=user_info.get("countryCode"),
            session_id=session_id,
            profile_name=user_info.get("profileName"),
            user_name=user_info.get("username"),
        )

    @staticmethod
    async def start_device_login(http_session: ClientSession) -> dict[str, Any]:
        """
        Begin the Tidal device authorization flow.

        Returns the device authorization response (device/user code, verification
        URLs, poll interval and expiry) to show to the user and hand to
        :meth:`poll_device_login`.

        :param http_session: The shared aiohttp session to use for the request.
        """
        client_id, _ = _v2_client_credentials()
        async with http_session.post(
            f"{AUTH_URL}/device_authorization",
            data={"client_id": client_id, "scope": AUTH_SCOPE},
        ) as response:
            device = await _read_json(response, "Device authorization")
            if response.status != 200:
                raise LoginFailed(f"Device authorization failed: {device}")
        return device

    @staticmethod
    async def poll_device_login(
        http_session: ClientSession, device: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Poll Tidal until the user approves the device code, returning the auth data.

        Polls indefinitely at the server-advised interval; the caller bounds the wait
        via the setup flow's step deadline (a fresh code is minted on expiry).

        :param http_session: The shared aiohttp session to use for the requests.
        :param device: The device authorization response from :meth:`start_device_login`.
        """
        client_id, client_secret = _v2_client_credentials()
        headers = _basic_auth_headers(client_id, client_secret)
        interval = int(device.get("interval", 2))
        while True:
            await asyncio.sleep(interval)
            data = {
                "client_id": client_id,
                "device_code": device["deviceCode"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "scope": AUTH_SCOPE,
            }
            async with http_session.post(
                f"{AUTH_URL}/token", data=data, headers=headers
            ) as response:
                token_data = await _read_json(response, "Device login")
                if response.status == 200:
                    return await TidalAuthManager._finalize_login(http_session, token_data)
            # Anything other than the two "keep waiting" signals is terminal.
            error = token_data.get("error")
            if error == "slow_down":
                # RFC 8628 section 3.5: increase the poll interval by 5 seconds.
                interval += 5
            elif error != "authorization_pending":
                raise LoginFailed(f"Device login failed: {token_data}")

    async def _perform_refresh(self) -> bool:
        """Perform the actual token refresh request."""
        if not self._auth_info:
            return False

        refresh_token = self._auth_info.get("refresh_token")
        if not refresh_token:
            return False

        client_id, client_secret = _v2_client_credentials()
        client_id = self._auth_info.get("client_id") or client_id

        data = {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "grant_type": "refresh_token",
            "scope": AUTH_SCOPE,
        }
        headers = _basic_auth_headers(client_id, client_secret)

        async with self.http_session.post(
            f"{AUTH_URL}/token", data=data, headers=headers
        ) as response:
            if response.status != 200:
                self.logger.error("Failed to refresh token: %s", await response.text())
                return False

            token_data = await response.json()

            # Update auth info
            self._auth_info["access_token"] = token_data["access_token"]
            if "refresh_token" in token_data:
                self._auth_info["refresh_token"] = token_data["refresh_token"]

            # Update expiration
            if "expires_in" in token_data:
                self._auth_info["expires_at"] = time.time() + token_data["expires_in"]

            # Store updated auth info
            self.update_config(self._auth_info)

            return True

    @staticmethod
    async def _finalize_login(
        http_session: ClientSession, token_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate device tokens, attach user/session info and an absolute expiry."""
        if not token_data.get("access_token") or not token_data.get("refresh_token"):
            raise LoginFailed("Failed to obtain authentication tokens from Tidal")

        headers = {"Authorization": f"Bearer {token_data['access_token']}"}
        async with http_session.get(SESSIONS_URL, headers=headers) as response:
            if response.status != 200:
                raise LoginFailed(f"Failed to get user info: {await response.text()}")
            user_info = await response.json()

        auth_data = {**token_data, **user_info}
        auth_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)
        return auth_data
