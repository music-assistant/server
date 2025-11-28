"""Authentication provider base classes and implementations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TypedDict, cast
from urllib.parse import urlparse

from hass_client import HomeAssistantClient
from hass_client.exceptions import BaseHassClientError
from hass_client.utils import base_url, get_auth_url, get_token, get_websocket_url
from music_assistant_models.auth import AuthProviderType, User, UserRole

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.helpers.datetime import utc

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.controllers.webserver.auth import AuthenticationManager
    from music_assistant.providers.hass import HomeAssistantProvider

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.auth")


class LoginRateLimiter:
    """Rate limiter for login attempts to prevent brute force attacks."""

    def __init__(self) -> None:
        """Initialize the rate limiter."""
        # Track failed attempts per username: {username: [timestamp1, timestamp2, ...]}
        self._failed_attempts: dict[str, list[datetime]] = {}
        # Time window for tracking attempts (30 minutes)
        self._tracking_window = timedelta(minutes=30)
        # Lock for thread-safe access to _failed_attempts
        self._lock = asyncio.Lock()

    def _cleanup_old_attempts(self, username: str) -> None:
        """
        Remove failed attempts outside the tracking window.

        :param username: The username to clean up.
        """
        if username not in self._failed_attempts:
            return

        cutoff_time = utc() - self._tracking_window
        self._failed_attempts[username] = [
            timestamp for timestamp in self._failed_attempts[username] if timestamp > cutoff_time
        ]

        # Remove username if no attempts left
        if not self._failed_attempts[username]:
            del self._failed_attempts[username]

    def get_delay(self, username: str) -> int:
        """
        Get the delay in seconds before next login attempt is allowed.

        Progressive delays based on failed attempts:
        - 1-2 attempts: no delay
        - 3-5 attempts: 30 seconds
        - 6-9 attempts: 60 seconds
        - 10-14 attempts: 120 seconds
        - 15+ attempts: 300 seconds (5 minutes)

        :param username: The username attempting to log in.
        :return: Delay in seconds (0 if no delay needed).
        """
        self._cleanup_old_attempts(username)

        if username not in self._failed_attempts:
            return 0

        attempt_count = len(self._failed_attempts[username])

        if attempt_count < 3:
            return 0
        if attempt_count < 6:
            return 30
        if attempt_count < 10:
            return 60
        if attempt_count < 15:
            return 120
        return 300  # 5 minutes max delay

    async def check_rate_limit(self, username: str) -> tuple[bool, int]:
        """
        Check if login attempt is allowed and apply delay if needed.

        :param username: The username attempting to log in.
        :return: Tuple of (allowed, delay_seconds). If not allowed, includes remaining delay.
        """
        async with self._lock:
            self._cleanup_old_attempts(username)

            if username not in self._failed_attempts or not self._failed_attempts[username]:
                return True, 0

            # Get the most recent failed attempt
            last_attempt = self._failed_attempts[username][-1]
            required_delay = self.get_delay(username)

            if required_delay == 0:
                return True, 0

            # Calculate how much time has passed since last attempt
            time_since_last = (utc() - last_attempt).total_seconds()

            if time_since_last < required_delay:
                # Still in cooldown period
                remaining_delay = int(required_delay - time_since_last)
                return False, remaining_delay

            return True, 0

    async def record_failed_attempt(self, username: str) -> None:
        """
        Record a failed login attempt.

        :param username: The username that failed to log in.
        """
        async with self._lock:
            self._cleanup_old_attempts(username)

            if username not in self._failed_attempts:
                self._failed_attempts[username] = []

            self._failed_attempts[username].append(utc())

            # Log warning for suspicious activity
            attempt_count = len(self._failed_attempts[username])
            if attempt_count == 10:
                LOGGER.warning(
                    "Suspicious login activity: 10 failed attempts for username '%s'", username
                )
            elif attempt_count == 20:
                LOGGER.warning(
                    "High suspicious login activity: 20 failed attempts for username '%s'. "
                    "Consider manually disabling this account.",
                    username,
                )

    async def clear_attempts(self, username: str) -> None:
        """
        Clear failed attempts for a username (called after successful login).

        :param username: The username to clear.
        """
        async with self._lock:
            if username in self._failed_attempts:
                del self._failed_attempts[username]


class LoginProviderConfig(TypedDict, total=False):
    """Base configuration for login providers."""

    allow_self_registration: bool


class HomeAssistantProviderConfig(LoginProviderConfig):
    """Configuration for Home Assistant OAuth provider."""

    ha_url: str


@dataclass
class AuthResult:
    """Result of an authentication attempt."""

    success: bool
    user: User | None = None
    error: str | None = None
    access_token: str | None = None
    return_url: str | None = None


class LoginProvider(ABC):
    """Base class for login providers."""

    def __init__(self, mass: MusicAssistant, provider_id: str, config: LoginProviderConfig) -> None:
        """
        Initialize login provider.

        :param mass: MusicAssistant instance.
        :param provider_id: Unique identifier for this provider instance.
        :param config: Provider-specific configuration.
        """
        self.mass = mass
        self.provider_id = provider_id
        self.config = config
        self.logger = LOGGER
        self.allow_self_registration = config.get("allow_self_registration", False)

    @property
    def auth_manager(self) -> AuthenticationManager:
        """Get auth manager from webserver."""
        return self.mass.webserver.auth

    @property
    @abstractmethod
    def provider_type(self) -> AuthProviderType:
        """Return the provider type."""

    @property
    @abstractmethod
    def requires_redirect(self) -> bool:
        """Return True if this provider requires OAuth redirect."""

    @abstractmethod
    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        """
        Authenticate user with provided credentials.

        :param credentials: Provider-specific credentials (username/password, OAuth code, etc).
        """

    async def get_authorization_url(
        self, redirect_uri: str, return_url: str | None = None
    ) -> str | None:
        """
        Get OAuth authorization URL if applicable.

        :param redirect_uri: The callback URL for OAuth flow.
        :param return_url: Optional URL to redirect to after successful login.
        """
        return None

    async def handle_oauth_callback(self, code: str, state: str, redirect_uri: str) -> AuthResult:
        """
        Handle OAuth callback if applicable.

        :param code: OAuth authorization code.
        :param state: OAuth state parameter for CSRF protection.
        :param redirect_uri: The callback URL.
        """
        return AuthResult(success=False, error="OAuth not supported by this provider")


class BuiltinLoginProvider(LoginProvider):
    """Built-in username/password login provider."""

    def __init__(self, mass: MusicAssistant, provider_id: str, config: LoginProviderConfig) -> None:
        """
        Initialize built-in login provider.

        :param mass: MusicAssistant instance.
        :param provider_id: Unique identifier for this provider instance.
        :param config: Provider-specific configuration.
        """
        super().__init__(mass, provider_id, config)
        self._rate_limiter = LoginRateLimiter()

    @property
    def provider_type(self) -> AuthProviderType:
        """Return the provider type."""
        return AuthProviderType.BUILTIN

    @property
    def requires_redirect(self) -> bool:
        """Return False - built-in provider doesn't need redirect."""
        return False

    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        """
        Authenticate user with username and password.

        :param credentials: Dict containing 'username' and 'password'.
        """
        username = credentials.get("username")
        password = credentials.get("password")

        if not username or not password:
            return AuthResult(success=False, error="Username and password required")

        # Check rate limit before attempting authentication
        allowed, remaining_delay = await self._rate_limiter.check_rate_limit(username)
        if not allowed:
            self.logger.warning(
                "Rate limit exceeded for username '%s'. %d seconds remaining.",
                username,
                remaining_delay,
            )
            return AuthResult(
                success=False,
                error=f"Too many failed attempts. Please try again in {remaining_delay} seconds.",
            )

        # First, look up user by username to get user_id
        # This is needed to create the password hash with user_id in the salt
        user_row = await self.auth_manager.database.get_row("users", {"username": username})
        if not user_row:
            # Record failed attempt even if username doesn't exist
            # This prevents username enumeration timing attacks
            await self._rate_limiter.record_failed_attempt(username)
            return AuthResult(success=False, error="Invalid username or password")

        user_id = user_row["user_id"]

        # Hash the password using user_id for enhanced security
        password_hash = self._hash_password(password, user_id)

        # Verify the password by checking if provider link exists
        user = await self.auth_manager.get_user_by_provider_link(
            AuthProviderType.BUILTIN, password_hash
        )

        if not user:
            # Record failed attempt
            await self._rate_limiter.record_failed_attempt(username)
            return AuthResult(success=False, error="Invalid username or password")

        # Check if user is enabled
        if not user.enabled:
            # Record failed attempt for disabled accounts too
            await self._rate_limiter.record_failed_attempt(username)
            return AuthResult(success=False, error="User account is disabled")

        # Successful login - clear any failed attempts
        await self._rate_limiter.clear_attempts(username)
        return AuthResult(success=True, user=user)

    async def create_user_with_password(
        self,
        username: str,
        password: str,
        role: UserRole = UserRole.USER,
        display_name: str | None = None,
        player_filter: list[str] | None = None,
        provider_filter: list[str] | None = None,
    ) -> User:
        """
        Create a new built-in user with password.

        :param username: The username.
        :param password: The password (will be hashed).
        :param role: The user role (default: USER).
        :param display_name: Optional display name.
        :param player_filter: Optional list of player IDs user has access to.
        :param provider_filter: Optional list of provider instance IDs user has access to.
        """
        # Create the user
        user = await self.auth_manager.create_user(
            username=username,
            role=role,
            display_name=display_name,
            player_filter=player_filter,
            provider_filter=provider_filter,
        )

        # Hash password using user_id for enhanced security
        password_hash = self._hash_password(password, user.user_id)
        await self.auth_manager.link_user_to_provider(user, AuthProviderType.BUILTIN, password_hash)

        return user

    async def change_password(self, user: User, old_password: str, new_password: str) -> bool:
        """
        Change user password.

        :param user: The user.
        :param old_password: Current password for verification.
        :param new_password: The new password.
        """
        # Verify old password first using user_id
        old_password_hash = self._hash_password(old_password, user.user_id)
        existing_user = await self.auth_manager.get_user_by_provider_link(
            AuthProviderType.BUILTIN, old_password_hash
        )

        if not existing_user or existing_user.user_id != user.user_id:
            return False

        # Update password link with new hash using user_id
        new_password_hash = self._hash_password(new_password, user.user_id)
        await self.auth_manager.update_provider_link(
            user, AuthProviderType.BUILTIN, new_password_hash
        )

        return True

    async def reset_password(self, user: User, new_password: str) -> None:
        """
        Reset user password (admin only - no old password verification).

        :param user: The user whose password to reset.
        :param new_password: The new password.
        """
        # Hash new password using user_id and update provider link
        new_password_hash = self._hash_password(new_password, user.user_id)
        await self.auth_manager.update_provider_link(
            user, AuthProviderType.BUILTIN, new_password_hash
        )

    def _hash_password(self, password: str, user_id: str) -> str:
        """
        Hash password with salt combining user ID and server ID.

        :param password: Plain text password.
        :param user_id: User ID to include in salt (random token for high entropy).
        """
        # Combine user_id (random) and server_id for maximum security
        salt = f"{user_id}:{self.mass.server_id}"
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), iterations=100000
        ).hex()


class HomeAssistantOAuthProvider(LoginProvider):
    """Home Assistant OAuth login provider."""

    @property
    def provider_type(self) -> AuthProviderType:
        """Return the provider type."""
        return AuthProviderType.HOME_ASSISTANT

    @property
    def requires_redirect(self) -> bool:
        """Return True - Home Assistant OAuth requires redirect."""
        return True

    async def authenticate(self, credentials: dict[str, Any]) -> AuthResult:
        """
        Not used for OAuth providers - use handle_oauth_callback instead.

        :param credentials: Not used.
        """
        return AuthResult(success=False, error="Use OAuth flow for Home Assistant authentication")

    async def _get_external_ha_url(self) -> str | None:
        """
        Get the external URL for Home Assistant from the config API.

        This is needed when MA runs as HA add-on and connects via internal docker network
        (http://supervisor/api) but needs the external URL for OAuth redirects.

        :return: External URL if available, otherwise None.
        """
        ha_url = cast("str", self.config.get("ha_url")) if self.config.get("ha_url") else None
        if not ha_url:
            return None

        # Check if we're using the internal supervisor URL
        if "supervisor" not in ha_url.lower():
            # Not using internal URL, return as-is
            return ha_url

        # We're using internal URL - try to get external URL from HA provider
        ha_provider = self.mass.get_provider("hass")
        if not ha_provider:
            # No HA provider available, use configured URL
            return ha_url

        ha_provider = cast("HomeAssistantProvider", ha_provider)

        try:
            # Access the hass client from the provider
            hass_client = ha_provider.hass
            if not hass_client or not hass_client.connected:
                return ha_url

            # Get network URLs from Home Assistant using WebSocket API
            # This command returns internal, external, and cloud URLs
            network_urls = await hass_client.send_command("network/url")

            if network_urls:
                # Priority: external > cloud > internal
                # External is the manually configured external URL
                # Cloud is the Nabu Casa cloud URL
                # Internal is the local network URL
                external_url = network_urls.get("external")
                cloud_url = network_urls.get("cloud")
                internal_url = network_urls.get("internal")

                # Use external URL first, then cloud, then internal
                final_url = cast("str", external_url or cloud_url or internal_url)
                if final_url:
                    self.logger.debug(
                        "Using HA URL for OAuth: %s (from network/url, configured: %s)",
                        final_url,
                        ha_url,
                    )
                    return final_url
        except Exception as err:
            self.logger.warning("Failed to fetch HA network URLs: %s", err, exc_info=True)

        # Fallback to configured URL
        return ha_url

    async def get_authorization_url(
        self, redirect_uri: str, return_url: str | None = None
    ) -> str | None:
        """
        Get Home Assistant OAuth authorization URL using hass_client.

        :param redirect_uri: The callback URL.
        :param return_url: Optional URL to redirect to after successful login.
        """
        # Get the correct HA URL (external URL if running as add-on)
        ha_url = await self._get_external_ha_url()
        if not ha_url:
            return None

        # If HA URL is still the internal supervisor URL (no external_url in HA config),
        # infer from redirect_uri (the URL user is accessing MA from)
        if "supervisor" in ha_url.lower():
            # Extract scheme and host from redirect_uri to build external HA URL
            parsed = urlparse(redirect_uri)
            # HA typically runs on port 8123, but use default ports for HTTPS (443) or HTTP (80)
            if parsed.scheme == "https":
                # HTTPS - use default port 443 (no port in URL)
                inferred_ha_url = f"{parsed.scheme}://{parsed.hostname}"
            else:
                # HTTP - assume HA runs on default port 8123
                inferred_ha_url = f"{parsed.scheme}://{parsed.hostname}:8123"

            self.logger.debug(
                "HA external_url not configured, inferring from callback URL: %s",
                inferred_ha_url,
            )
            ha_url = inferred_ha_url

        state = secrets.token_urlsafe(32)
        # Store state and return_url for verification and final redirect
        self._oauth_state = state
        self._oauth_return_url = return_url

        # Use base_url of callback as client_id (same as HA provider does)
        client_id = base_url(redirect_uri)

        # Use hass_client's get_auth_url utility
        return cast(
            "str",
            get_auth_url(
                ha_url,
                redirect_uri,
                client_id=client_id,
                state=state,
            ),
        )

    def _decode_ha_jwt_token(self, access_token: str) -> tuple[str | None, str | None]:
        """
        Decode Home Assistant JWT token to extract user ID and name.

        :param access_token: The JWT access token from Home Assistant.
        :return: Tuple of (user_id, username) or (None, None) if decoding fails.
        """
        try:
            # JWT tokens have 3 parts separated by dots: header.payload.signature
            parts = access_token.split(".")
            if len(parts) >= 2:
                # Decode the payload (second part)
                # Add padding if needed (JWT base64 may not be padded)
                payload = parts[1]
                payload += "=" * (4 - len(payload) % 4)
                decoded = base64.urlsafe_b64decode(payload)
                token_data = json.loads(decoded)

                # Home Assistant JWT tokens use 'iss' as the user ID
                ha_user_id: str | None = token_data.get("iss")

                if not ha_user_id:
                    # Fallback to 'sub' if 'iss' is not present
                    ha_user_id = token_data.get("sub")

                # Try to extract username from token (name, username, or other fields)
                username = token_data.get("name") or token_data.get("username")

                if ha_user_id:
                    return str(ha_user_id), username
                return None, None
        except Exception as decode_error:
            self.logger.error("Failed to decode HA JWT token: %s", decode_error)

        return None, None

    async def _fetch_ha_user_via_websocket(
        self, ha_url: str, access_token: str
    ) -> tuple[str | None, str | None]:
        """
        Fetch user information from Home Assistant via WebSocket.

        :param ha_url: Home Assistant URL.
        :param access_token: Access token for WebSocket authentication.
        :return: Tuple of (username, display_name) or (None, None) if fetch fails.
        """
        ws_url = get_websocket_url(ha_url)

        try:
            # Use context manager to automatically handle connect/disconnect
            async with HomeAssistantClient(ws_url, access_token, self.mass.http_session) as client:
                # Use the auth/current_user command to get user details
                result = await client.send_command("auth/current_user")

                if result:
                    # Extract username and display name from response
                    username = result.get("name") or result.get("username")
                    display_name = result.get("name")
                    if username:
                        return username, display_name

                self.logger.warning("auth/current_user returned no user data")
                return None, None

        except BaseHassClientError as ws_error:
            self.logger.error("Failed to fetch HA user via WebSocket: %s", ws_error)
            return None, None

    async def _get_or_create_user(
        self, username: str, display_name: str | None, ha_user_id: str
    ) -> User | None:
        """
        Get or create a user for Home Assistant OAuth authentication.

        :param username: Username from Home Assistant.
        :param display_name: Display name from Home Assistant.
        :param ha_user_id: Home Assistant user ID.
        :return: User object or None if creation failed.
        """
        # Check if user already linked to HA
        user = await self.auth_manager.get_user_by_provider_link(
            AuthProviderType.HOME_ASSISTANT, ha_user_id
        )
        if user:
            return user

        # Check if a user with this username already exists (from built-in provider)
        user_row = await self.auth_manager.database.get_row("users", {"username": username})
        if user_row:
            # User exists with this username - link them to HA provider
            user_dict = dict(user_row)
            existing_user = User(
                user_id=user_dict["user_id"],
                username=user_dict["username"],
                role=UserRole(user_dict["role"]),
                enabled=bool(user_dict["enabled"]),
                created_at=datetime.fromisoformat(user_dict["created_at"]),
                display_name=user_dict["display_name"],
                avatar_url=user_dict["avatar_url"],
            )

            # Link existing user to Home Assistant
            await self.auth_manager.link_user_to_provider(
                existing_user, AuthProviderType.HOME_ASSISTANT, ha_user_id
            )

            self.logger.debug("Linked existing user '%s' to Home Assistant provider", username)
            return existing_user

        # New HA user - check if self-registration allowed
        if not self.allow_self_registration:
            return None

        # Create new user with USER role
        user = await self.auth_manager.create_user(
            username=username,
            role=UserRole.USER,
            display_name=display_name or username,
        )

        # Link to Home Assistant
        await self.auth_manager.link_user_to_provider(
            user, AuthProviderType.HOME_ASSISTANT, ha_user_id
        )

        return user

    async def handle_oauth_callback(self, code: str, state: str, redirect_uri: str) -> AuthResult:
        """
        Handle Home Assistant OAuth callback using hass_client.

        :param code: OAuth authorization code.
        :param state: OAuth state parameter.
        :param redirect_uri: The callback URL.
        """
        # Verify state
        if not hasattr(self, "_oauth_state") or state != self._oauth_state:
            return AuthResult(success=False, error="Invalid state parameter")

        # Get the correct HA URL (external URL if running as add-on)
        # This must be the same URL used in get_authorization_url
        ha_url = await self._get_external_ha_url()
        if not ha_url:
            return AuthResult(success=False, error="Home Assistant URL not configured")

        try:
            # Use base_url of callback as client_id (same as HA provider does)
            client_id = base_url(redirect_uri)

            # Use hass_client's get_token utility - no client_secret needed!
            try:
                token_details = await get_token(ha_url, code, client_id=client_id)
            except Exception as token_error:
                self.logger.error(
                    "Failed to get token from HA: %s (client_id: %s, ha_url: %s)",
                    token_error,
                    client_id,
                    ha_url,
                )
                return AuthResult(
                    success=False, error=f"Failed to exchange OAuth code: {token_error}"
                )

            access_token = token_details.get("access_token")
            if not access_token:
                return AuthResult(success=False, error="No access token received from HA")

            # Decode JWT token to get HA user ID
            ha_user_id, _ = self._decode_ha_jwt_token(access_token)
            if not ha_user_id:
                return AuthResult(success=False, error="Failed to decode token")

            # Fetch user information from HA via WebSocket
            username, display_name = await self._fetch_ha_user_via_websocket(ha_url, access_token)

            # If we couldn't get username from WebSocket, fail authentication
            if not username:
                return AuthResult(
                    success=False,
                    error="Failed to get username from Home Assistant",
                )

            # Get or create user
            user = await self._get_or_create_user(username, display_name, ha_user_id)

            # Get stored return_url from OAuth state
            return_url = getattr(self, "_oauth_return_url", None)

            if not user:
                return AuthResult(
                    success=False,
                    error="Self-registration is disabled. Please contact an administrator.",
                )

            return AuthResult(success=True, user=user, return_url=return_url)

        except Exception as e:
            self.logger.exception("Error during Home Assistant OAuth callback")
            return AuthResult(success=False, error=str(e))
