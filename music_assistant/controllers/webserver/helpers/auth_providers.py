"""Authentication provider base classes and implementations."""

from __future__ import annotations

import hashlib
import logging
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, cast

from hass_client.utils import base_url, get_auth_url, get_token

from music_assistant.constants import MASS_LOGGER_NAME
from music_assistant.models.auth import AuthProviderType, User, UserRole

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.controllers.webserver.auth import AuthenticationManager

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.auth")


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

    async def get_authorization_url(self, redirect_uri: str) -> str | None:
        """
        Get OAuth authorization URL if applicable.

        :param redirect_uri: The callback URL for OAuth flow.
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

        # Hash the password to use as provider_user_id
        password_hash = self._hash_password(password, username)

        # Try to find user by provider link
        user = await self.auth_manager.get_user_by_provider_link(
            AuthProviderType.BUILTIN, password_hash
        )

        if not user:
            return AuthResult(success=False, error="Invalid username or password")

        # Check if user is enabled
        if not user.enabled:
            return AuthResult(success=False, error="User account is disabled")

        return AuthResult(success=True, user=user)

    async def create_user_with_password(
        self, username: str, password: str, role: UserRole = UserRole.USER
    ) -> User:
        """
        Create a new built-in user with password.

        :param username: The username.
        :param password: The password (will be hashed).
        :param role: The user role (default: USER).
        """
        # Create the user
        user = await self.auth_manager.create_user(
            username=username,
            role=role,
        )

        # Hash password and link to provider
        password_hash = self._hash_password(password, username)
        await self.auth_manager.link_user_to_provider(user, AuthProviderType.BUILTIN, password_hash)

        return user

    async def change_password(self, user: User, old_password: str, new_password: str) -> bool:
        """
        Change user password.

        :param user: The user.
        :param old_password: Current password for verification.
        :param new_password: The new password.
        """
        # Verify old password first
        old_password_hash = self._hash_password(old_password, user.username)
        existing_user = await self.auth_manager.get_user_by_provider_link(
            AuthProviderType.BUILTIN, old_password_hash
        )

        if not existing_user or existing_user.user_id != user.user_id:
            return False

        # Update password link
        new_password_hash = self._hash_password(new_password, user.username)
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
        # Hash new password and update provider link
        new_password_hash = self._hash_password(new_password, user.username)
        await self.auth_manager.update_provider_link(
            user, AuthProviderType.BUILTIN, new_password_hash
        )

    def _hash_password(self, password: str, salt: str) -> str:
        """
        Hash password with salt.

        :param password: Plain text password.
        :param salt: Salt (using username as salt).
        """
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

    async def get_authorization_url(self, redirect_uri: str) -> str | None:
        """
        Get Home Assistant OAuth authorization URL using hass_client.

        :param redirect_uri: The callback URL.
        """
        ha_url = self.config.get("ha_url")
        if not ha_url:
            return None

        state = secrets.token_urlsafe(32)
        # Store state for verification
        self._oauth_state = state

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

        ha_url = self.config.get("ha_url")
        if not ha_url:
            return AuthResult(success=False, error="Home Assistant URL not configured")

        try:
            # Use base_url of callback as client_id (same as HA provider does)
            client_id = base_url(redirect_uri)

            # Use hass_client's get_token utility - no client_secret needed!
            token_details = await get_token(ha_url, code, client_id=client_id)
            access_token = token_details.get("access_token")

            # Get user info from Home Assistant
            userinfo_url = f"{ha_url}/api/"
            headers = {"Authorization": f"Bearer {access_token}"}
            async with self.mass.http_session.get(userinfo_url, headers=headers) as response:
                if response.status != 200:
                    return AuthResult(success=False, error="Failed to get user info from HA")

            # Get current user info
            userinfo_url = f"{ha_url}/api/auth/current_user"
            async with self.mass.http_session.get(userinfo_url, headers=headers) as response:
                user_info = await response.json()

            ha_user_id = user_info.get("id")
            username = user_info.get("username") or user_info.get("name")

            if not ha_user_id or not username:
                return AuthResult(success=False, error="Failed to get user information from HA")

            # Check if user already linked to HA
            user = await self.auth_manager.get_user_by_provider_link(
                AuthProviderType.HOME_ASSISTANT, ha_user_id
            )

            if user:
                # Existing user
                return AuthResult(success=True, user=user)

            # New HA user - check if self-registration allowed
            if not self.allow_self_registration:
                return AuthResult(
                    success=False,
                    error="Self-registration is disabled. Please contact an administrator.",
                )

            # Create new user with USER role
            user = await self.auth_manager.create_user(
                username=username,
                role=UserRole.USER,
                display_name=user_info.get("name"),
            )

            # Link to Home Assistant
            await self.auth_manager.link_user_to_provider(
                user, AuthProviderType.HOME_ASSISTANT, ha_user_id
            )

            return AuthResult(success=True, user=user)

        except Exception as e:
            self.logger.exception("Error during Home Assistant OAuth callback")
            return AuthResult(success=False, error=str(e))
