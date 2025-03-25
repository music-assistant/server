"""Authentication manager for Tidal integration."""

import json
import random
import time
import urllib
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any

import pkce
from aiohttp import ClientSession
from music_assistant_models.enums import EventType
from music_assistant_models.errors import LoginFailed

from music_assistant.helpers.app_vars import app_var  # type: ignore[attr-defined]

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# Configuration constants
TOKEN_TYPE = "Bearer"
AUTH_URL = "https://auth.tidal.com/v1/oauth2"
REDIRECT_URI = "https://tidal.com/android/login/auth"


@dataclass
class TidalUser:
    """Represent a Tidal user with their associated account information."""

    user_id: str | None = None
    country_code: str | None = None
    session_id: str | None = None
    profile_name: str | None = None
    user_name: str | None = None
    email: str | None = None


class ManualAuthenticationHelper:
    """Helper for authentication flows that require manual user intervention.

    For Tidal where the OAuth flow doesn't redirect to our callback,
    but instead requires the user to manually copy a URL after authentication.
    """

    def __init__(self, mass: "MusicAssistant", session_id: str) -> None:
        """Initialize the Manual Authentication Helper."""
        self.mass = mass
        self.session_id = session_id

    async def __aenter__(self) -> "ManualAuthenticationHelper":
        """Enter context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit context manager."""
        return None

    def send_url(self, auth_url: str) -> None:
        """Send the URL to the user for authentication."""
        self.mass.signal_event(EventType.AUTH_SESSION, self.session_id, auth_url)


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
        self._auth_info = None
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
        expires_at = self._auth_info.get("expires_at", 0)  # type: ignore[unreachable]
        if expires_at > time.time() - 60:
            return True

        # Need to refresh token
        return await self.refresh_token()

    async def refresh_token(self) -> bool:
        """Refresh the auth token."""
        if not self._auth_info:
            return False

        refresh_token = self._auth_info.get("refresh_token")  # type: ignore[unreachable]
        if not refresh_token:
            return False

        client_id = self._auth_info.get("client_id", app_var(9))

        data = {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "grant_type": "refresh_token",
            "scope": "r_usr w_usr w_sub",
        }

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

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
    async def generate_auth_url(auth_helper: ManualAuthenticationHelper, quality: str) -> str:
        """Generate the Tidal authentication URL."""
        # Generate PKCE challenge
        code_verifier, code_challenge = pkce.generate_pkce_pair()
        # Generate unique client key
        client_unique_key = format(random.getrandbits(64), "02x")
        # Store these values for later use
        auth_params = {
            "code_verifier": code_verifier,
            "client_unique_key": client_unique_key,
            "client_id": app_var(9),
            "client_secret": app_var(10),
            "quality": quality,
        }

        # Create auth URL
        params = {
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "client_id": auth_params["client_id"],
            "lang": "EN",
            "appMode": "android",
            "client_unique_key": client_unique_key,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "restrict_signup": "true",
        }

        url = f"https://login.tidal.com/authorize?{urllib.parse.urlencode(params)}"

        # Send URL to user
        auth_helper.mass.loop.call_soon_threadsafe(auth_helper.send_url, url)

        # Return serialized auth params
        return json.dumps(auth_params)

    @staticmethod
    async def process_pkce_login(
        http_session: ClientSession, base64_auth_params: str, redirect_url: str
    ) -> dict[str, Any]:
        """Process TIDAL authentication with PKCE flow."""
        # Parse the stored auth parameters
        try:
            auth_params = json.loads(base64_auth_params)
        except json.JSONDecodeError as err:
            raise LoginFailed("Invalid authentication data") from err

        # Extract required parameters
        code_verifier = auth_params.get("code_verifier")
        client_unique_key = auth_params.get("client_unique_key")
        client_secret = auth_params.get("client_secret")
        client_id = auth_params.get("client_id")
        quality = auth_params.get("quality")

        if not code_verifier or not client_unique_key:
            raise LoginFailed("Missing required authentication parameters")

        # Extract the authorization code from the redirect URL
        parsed_url = urllib.parse.urlparse(redirect_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        code = query_params.get("code", [""])[0]

        if not code:
            raise LoginFailed("No authorization code found in redirect URL")

        # Prepare the token exchange request
        token_url = f"{AUTH_URL}/token"
        data = {
            "code": code,
            "client_id": client_id,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "scope": "r_usr w_usr w_sub",
            "code_verifier": code_verifier,
            "client_unique_key": client_unique_key,
        }

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        # Make the token exchange request
        async with http_session.post(token_url, data=data, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise LoginFailed(f"Token exchange failed: {error_text}")

            token_data = await response.json()

        # Validate we have authentication data
        if not token_data.get("access_token") or not token_data.get("refresh_token"):
            raise LoginFailed("Failed to obtain authentication tokens from Tidal")

        # Get user information using the new token
        headers = {"Authorization": f"Bearer {token_data['access_token']}"}
        sessions_url = "https://api.tidal.com/v1/sessions"

        # Again use mass.http_session
        async with http_session.get(sessions_url, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise LoginFailed(f"Failed to get user info: {error_text}")

            user_info = await response.json()

        # Combine token and user info, add expiration time
        auth_data = {**token_data, **user_info}

        # Add standard fields used by TidalProvider
        auth_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)
        auth_data["quality"] = quality
        auth_data["client_id"] = client_id
        auth_data["client_secret"] = client_secret

        return auth_data
