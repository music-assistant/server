"""Authentication manager for Music Assistant webserver.

This is NOT a CoreController - it's a component of the webserver controller.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from music_assistant.constants import CONF_ONBOARD_DONE, MASS_LOGGER_NAME
from music_assistant.controllers.webserver.helpers.auth_providers import (
    AuthResult,
    BuiltinLoginProvider,
    HomeAssistantOAuthProvider,
    HomeAssistantProviderConfig,
    LoginProvider,
    LoginProviderConfig,
)
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.datetime import utc
from music_assistant.models.auth import (
    AuthProviderType,
    AuthToken,
    User,
    UserAuthProvider,
    UserRole,
)

if TYPE_CHECKING:
    from music_assistant.controllers.webserver import WebserverController

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.auth")

# Token expiration (30 days by default)
TOKEN_EXPIRATION_DAYS = 30

# Config keys (defined in controller.py to avoid circular import)
CONF_AUTH_ALLOW_SELF_REGISTRATION = "auth_allow_self_registration"
CONF_AUTH_HA_ENABLED = "auth_ha_enabled"


class AuthenticationManager:
    """Manager for authentication and user management (part of webserver controller)."""

    def __init__(self, webserver: WebserverController) -> None:
        """
        Initialize the authentication manager.

        :param webserver: WebserverController instance.
        """
        self.webserver = webserver
        self.mass = webserver.mass
        self.database: DatabaseConnection = None  # type: ignore[assignment]
        self.login_providers: dict[str, LoginProvider] = {}
        self.logger = LOGGER

    async def setup(self) -> None:
        """Initialize the authentication manager."""
        # Get auth settings from config
        allow_self_registration = self.webserver.config.get_value(CONF_AUTH_ALLOW_SELF_REGISTRATION)
        assert isinstance(allow_self_registration, bool)

        # Setup database
        db_path = self.mass.storage_path + "/auth.db"
        self.database = DatabaseConnection(db_path)
        await self.database.setup()

        # Create database schema
        await self._create_database_schema()

        # Setup login providers based on config
        await self._setup_login_providers(allow_self_registration)

        # Migration: Reset onboard_done if no users exist
        # This handles existing setups where authentication was optional
        if self.mass.config.onboard_done and not await self.has_users():
            self.logger.warning(
                "Authentication is now mandatory but no users exist. "
                "Resetting onboard_done to redirect to setup."
            )
            self.mass.config.set(CONF_ONBOARD_DONE, False)
            self.mass.config.save(immediate=True)

        self.logger.info(
            "Authentication manager initialized (providers=%d)", len(self.login_providers)
        )

    async def close(self) -> None:
        """Cleanup on exit."""
        if self.database:
            await self.database.close()

    async def _create_database_schema(self) -> None:
        """Create the database schema for authentication."""
        # Users table (decoupled from auth providers)
        await self.database.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                display_name TEXT,
                avatar_url TEXT
            )
            """
        )

        # User auth provider links (many-to-many)
        await self.database.execute(
            """
            CREATE TABLE IF NOT EXISTS user_auth_providers (
                link_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                provider_type TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(provider_type, provider_user_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
        )

        # Auth tokens table
        await self.database.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                last_used_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
        )

        # Create indexes
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_auth_providers_user "
            "ON user_auth_providers(user_id)"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_auth_providers_provider "
            "ON user_auth_providers(provider_type, provider_user_id)"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS idx_tokens_user ON auth_tokens(user_id)"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS idx_tokens_hash ON auth_tokens(token_hash)"
        )

        await self.database.commit()

    async def _setup_login_providers(self, allow_self_registration: bool) -> None:
        """
        Set up available login providers based on configuration.

        :param allow_self_registration: Whether to allow self-registration via OAuth.
        """
        # Always enable built-in provider
        builtin_config: LoginProviderConfig = {"allow_self_registration": False}
        self.login_providers["builtin"] = BuiltinLoginProvider(self.mass, "builtin", builtin_config)

        # Home Assistant OAuth provider
        # Requires the HA provider (plugin) to be configured
        if self.webserver.config.get_value(CONF_AUTH_HA_ENABLED):
            # Find the HA provider
            ha_provider = None
            for provider in self.mass.providers:
                if provider.domain == "hass" and provider.available:
                    ha_provider = provider
                    break

            if not ha_provider:
                self.logger.warning(
                    "Home Assistant authentication is enabled but the Home Assistant provider "
                    "is not configured or not available. Please configure the Home Assistant "
                    "provider first."
                )
            else:
                # Get URL from the HA provider config
                ha_url = ha_provider.config.get_value("url")
                assert isinstance(ha_url, str)
                ha_config: HomeAssistantProviderConfig = {
                    "ha_url": ha_url,
                    "allow_self_registration": allow_self_registration,
                }
                self.login_providers["homeassistant"] = HomeAssistantOAuthProvider(
                    self.mass, "homeassistant", ha_config
                )
                self.logger.info(
                    "Home Assistant OAuth provider enabled (using URL from HA provider: %s)",
                    ha_url,
                )

    async def has_users(self) -> bool:
        """Check if any users exist in the system."""
        count = await self.database.get_count("users")
        return count > 0

    async def authenticate_with_credentials(
        self, provider_id: str, credentials: dict[str, Any]
    ) -> AuthResult:
        """
        Authenticate a user with credentials.

        :param provider_id: The login provider ID.
        :param credentials: Provider-specific credentials.
        """
        provider = self.login_providers.get(provider_id)
        if not provider:
            return AuthResult(success=False, error="Invalid provider")

        return await provider.authenticate(credentials)

    async def authenticate_with_token(self, token: str) -> User | None:
        """
        Authenticate a user with an access token.

        :param token: The access token.
        """
        # Hash the token to look it up
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        # Find token in database
        token_row = await self.database.get_row("auth_tokens", {"token_hash": token_hash})
        if not token_row:
            return None

        # Check if token is expired
        if token_row["expires_at"]:
            expires_at = datetime.fromisoformat(token_row["expires_at"])
            if utc() > expires_at:
                # Token expired, delete it
                await self.database.delete("auth_tokens", {"token_id": token_row["token_id"]})
                return None

        # Update last used timestamp
        await self.database.update(
            "auth_tokens",
            {"token_id": token_row["token_id"]},
            {"last_used_at": utc().isoformat()},
        )

        # Get user
        return await self.get_user(token_row["user_id"])

    async def get_user(self, user_id: str) -> User | None:
        """
        Get a user by ID.

        :param user_id: The user ID.
        """
        user_row = await self.database.get_row("users", {"user_id": user_id})
        if not user_row or not user_row["enabled"]:
            return None

        return User(
            user_id=user_row["user_id"],
            username=user_row["username"],
            role=UserRole(user_row["role"]),
            enabled=bool(user_row["enabled"]),
            created_at=datetime.fromisoformat(user_row["created_at"]),
            display_name=user_row.get("display_name"),
            avatar_url=user_row.get("avatar_url"),
        )

    async def get_user_by_provider_link(
        self, provider_type: AuthProviderType, provider_user_id: str
    ) -> User | None:
        """
        Get user by their provider link.

        :param provider_type: The auth provider type.
        :param provider_user_id: The user ID from the provider.
        """
        link_row = await self.database.get_row(
            "user_auth_providers",
            {
                "provider_type": provider_type.value,
                "provider_user_id": provider_user_id,
            },
        )
        if not link_row:
            return None

        return await self.get_user(link_row["user_id"])

    async def create_user(
        self,
        username: str,
        role: UserRole = UserRole.USER,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> User:
        """
        Create a new user.

        :param username: The username.
        :param role: The user role (default: USER).
        :param display_name: Optional display name.
        :param avatar_url: Optional avatar URL.
        """
        user_id = secrets.token_urlsafe(32)
        created_at = utc()
        user_data = {
            "user_id": user_id,
            "username": username,
            "role": role.value,
            "enabled": True,
            "created_at": created_at.isoformat(),
            "display_name": display_name,
            "avatar_url": avatar_url,
        }

        await self.database.insert("users", user_data)

        return User(
            user_id=user_id,
            username=username,
            role=role,
            enabled=True,
            created_at=created_at,
            display_name=display_name,
            avatar_url=avatar_url,
        )

    async def link_user_to_provider(
        self,
        user: User,
        provider_type: AuthProviderType,
        provider_user_id: str,
    ) -> UserAuthProvider:
        """
        Link a user to an authentication provider.

        :param user: The user to link.
        :param provider_type: The provider type.
        :param provider_user_id: The user ID from the provider (e.g., password hash, OAuth ID).
        """
        link_id = secrets.token_urlsafe(32)
        created_at = utc()
        link_data = {
            "link_id": link_id,
            "user_id": user.user_id,
            "provider_type": provider_type.value,
            "provider_user_id": provider_user_id,
            "created_at": created_at.isoformat(),
        }

        await self.database.insert("user_auth_providers", link_data)

        return UserAuthProvider(
            link_id=link_id,
            user_id=user.user_id,
            provider_type=provider_type,
            provider_user_id=provider_user_id,
            created_at=created_at,
        )

    async def update_provider_link(
        self,
        user: User,
        provider_type: AuthProviderType,
        provider_user_id: str,
    ) -> None:
        """
        Update a user's provider link (e.g., change password).

        :param user: The user.
        :param provider_type: The provider type.
        :param provider_user_id: The new provider user ID (e.g., new password hash).
        """
        # Find existing link
        link_row = await self.database.get_row(
            "user_auth_providers",
            {
                "user_id": user.user_id,
                "provider_type": provider_type.value,
            },
        )

        if link_row:
            # Update existing link
            await self.database.update(
                "user_auth_providers",
                {"link_id": link_row["link_id"]},
                {"provider_user_id": provider_user_id},
            )
        else:
            # Create new link
            await self.link_user_to_provider(user, provider_type, provider_user_id)

    async def create_token(self, user: User, name: str, expires_in_days: int | None = None) -> str:
        """
        Create a new access token for a user.

        :param user: The user to create the token for.
        :param name: A name/description for the token (e.g., device name).
        :param expires_in_days: Optional expiration in days (default: 30 days).
        """
        # Generate token
        token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        # Calculate expiration
        if expires_in_days is None:
            expires_in_days = TOKEN_EXPIRATION_DAYS
        created_at = utc()
        expires_at = utc() + timedelta(days=expires_in_days) if expires_in_days else None

        # Store token
        token_data = {
            "token_id": secrets.token_urlsafe(32),
            "user_id": user.user_id,
            "token_hash": token_hash,
            "name": name,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
        await self.database.insert("auth_tokens", token_data)

        return token

    async def revoke_token(self, token_id: str, user: User) -> bool:
        """
        Revoke an access token.

        :param token_id: The token ID to revoke.
        :param user: The user revoking the token (must own it or be admin).
        """
        token_row = await self.database.get_row("auth_tokens", {"token_id": token_id})
        if not token_row:
            return False

        # Check permissions - users can only revoke their own tokens unless admin
        if token_row["user_id"] != user.user_id and user.role != UserRole.ADMIN:
            return False

        await self.database.delete("auth_tokens", {"token_id": token_id})
        return True

    async def get_user_tokens(self, user: User) -> list[AuthToken]:
        """
        Get all tokens for a user.

        :param user: The user to get tokens for.
        """
        token_rows = await self.database.get_rows(
            "auth_tokens", {"user_id": user.user_id}, limit=100
        )
        return [AuthToken.from_dict(dict(row)) for row in token_rows]

    async def list_users(self) -> list[User]:
        """Get all users."""
        user_rows = await self.database.get_rows("users", limit=1000)
        return [
            User(
                user_id=row["user_id"],
                username=row["username"],
                role=UserRole(row["role"]),
                enabled=bool(row["enabled"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                display_name=row.get("display_name"),
                avatar_url=row.get("avatar_url"),
            )
            for row in user_rows
        ]

    async def update_user_role(self, user_id: str, new_role: UserRole, admin_user: User) -> bool:
        """
        Update a user's role (admin only).

        :param user_id: The user ID to update.
        :param new_role: The new role to assign.
        :param admin_user: The admin user performing the action.
        """
        if admin_user.role != UserRole.ADMIN:
            return False

        user_row = await self.database.get_row("users", {"user_id": user_id})
        if not user_row:
            return False

        await self.database.update(
            "users",
            {"user_id": user_id},
            {"role": new_role.value},
        )
        return True

    async def enable_user(self, user_id: str, admin_user: User) -> bool:
        """
        Enable a user (admin only).

        :param user_id: The user ID to enable.
        :param admin_user: The admin user performing the action.
        """
        if admin_user.role != UserRole.ADMIN:
            return False

        await self.database.update(
            "users",
            {"user_id": user_id},
            {"enabled": 1},
        )
        return True

    async def disable_user(self, user_id: str, admin_user: User) -> bool:
        """
        Disable a user (admin only).

        :param user_id: The user ID to disable.
        :param admin_user: The admin user performing the action.
        """
        if admin_user.role != UserRole.ADMIN:
            return False

        # Cannot disable yourself
        if user_id == admin_user.user_id:
            return False

        await self.database.update(
            "users",
            {"user_id": user_id},
            {"enabled": 0},
        )
        return True

    async def get_login_providers(self) -> list[dict[str, Any]]:
        """Get list of available login providers."""
        providers = []
        for provider_id, provider in self.login_providers.items():
            providers.append(
                {
                    "provider_id": provider_id,
                    "provider_type": provider.provider_type.value,
                    "requires_redirect": provider.requires_redirect,
                }
            )
        return providers

    async def get_authorization_url(self, provider_id: str, redirect_uri: str) -> str | None:
        """
        Get OAuth authorization URL for a provider.

        :param provider_id: The provider ID.
        :param redirect_uri: The callback URL.
        """
        provider = self.login_providers.get(provider_id)
        if not provider or not provider.requires_redirect:
            return None

        return await provider.get_authorization_url(redirect_uri)

    async def handle_oauth_callback(
        self, provider_id: str, code: str, state: str, redirect_uri: str
    ) -> AuthResult:
        """
        Handle OAuth callback.

        :param provider_id: The provider ID.
        :param code: OAuth authorization code.
        :param state: OAuth state parameter.
        :param redirect_uri: The callback URL.
        """
        provider = self.login_providers.get(provider_id)
        if not provider:
            return AuthResult(success=False, error="Invalid provider")

        return await provider.handle_oauth_callback(code, state, redirect_uri)

    async def create_long_lived_token(self, user: User, name: str) -> str:
        """
        Create a long-lived access token (no expiration) for external apps/integrations.

        This is similar to Home Assistant's long-lived access tokens - they never expire
        and are intended for external applications like the Home Assistant integration,
        mobile apps, etc. Users can manage and revoke these tokens at any time.

        :param user: The user to create the token for.
        :param name: A name/description for the token (e.g., "Home Assistant", "Mobile App").
        """
        # Create a token with no expiration
        token = await self.create_token(user, name, expires_in_days=None)

        self.logger.info("Created long-lived token '%s' for user '%s'", name, user.username)
        return token
