"""Authentication manager for Music Assistant webserver."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import (
    AuthProviderType,
    AuthToken,
    User,
    UserAuthProvider,
    UserRole,
)
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    InsufficientPermissions,
    InvalidDataError,
)

from music_assistant.constants import (
    CONF_AUTH_ALLOW_SELF_REGISTRATION,
    CONF_ONBOARD_DONE,
    HOMEASSISTANT_SYSTEM_USER,
    MASS_LOGGER_NAME,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_token,
    get_current_user,
)
from music_assistant.controllers.webserver.helpers.auth_providers import (
    AuthResult,
    BuiltinLoginProvider,
    HomeAssistantOAuthProvider,
    HomeAssistantProviderConfig,
    LoginProvider,
    LoginProviderConfig,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import json_dumps, json_loads

if TYPE_CHECKING:
    from music_assistant.controllers.webserver import WebserverController

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.auth")

# Database schema version
DB_SCHEMA_VERSION = 2

# Token expiration constants (in days)
TOKEN_SHORT_LIVED_EXPIRATION = 30  # Short-lived tokens (auto-renewing on use)
TOKEN_LONG_LIVED_EXPIRATION = 3650  # Long-lived tokens (10 years, no auto-renewal)


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

        # Create database schema and handle migrations
        await self._setup_database()

        # Setup login providers based on config
        await self._setup_login_providers(allow_self_registration)

        # Migration: Reset onboard_done if no users exist
        # This handles existing setups where authentication was optional
        if self.mass.config.onboard_done and not await self.has_users():
            self.logger.warning(
                "Authentication is mandatory but no users exist. "
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

    async def _setup_database(self) -> None:
        """Set up database schema and handle migrations."""
        # Always create tables if they don't exist
        await self._create_database_tables()

        # Check current schema version
        try:
            if db_row := await self.database.get_row("settings", {"key": "schema_version"}):
                prev_version = int(db_row["value"])
            else:
                prev_version = 0
        except (KeyError, ValueError, Exception):
            # settings table doesn't exist yet or other error
            prev_version = 0

        # Perform migration if needed
        if prev_version < DB_SCHEMA_VERSION:
            self.logger.warning(
                "Performing database migration from schema version %s to %s",
                prev_version,
                DB_SCHEMA_VERSION,
            )
            await self._migrate_database(prev_version)

        # Store current schema version
        await self.database.insert_or_replace(
            "settings",
            {"key": "schema_version", "value": str(DB_SCHEMA_VERSION), "type": "int"},
        )

        # Create indexes
        await self._create_database_indexes()
        await self.database.commit()

    async def _create_database_tables(self) -> None:
        """Create database tables."""
        # Settings table (for schema version and other settings)
        await self.database.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                type TEXT
            )
            """
        )

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
                avatar_url TEXT,
                preferences TEXT DEFAULT '{}'
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
                is_long_lived INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
        )

        await self.database.commit()

    async def _create_database_indexes(self) -> None:
        """Create database indexes."""
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

    async def _migrate_database(self, from_version: int) -> None:
        """Perform database migration.

        :param from_version: The schema version to migrate from.
        """
        self.logger.info(
            "Migrating auth database from version %s to %s", from_version, DB_SCHEMA_VERSION
        )
        # Migration to version 2: Recreate tables due to password salt breaking change
        if from_version < 2:
            # Drop all auth-related tables
            await self.database.execute("DROP TABLE IF EXISTS auth_tokens")
            await self.database.execute("DROP TABLE IF EXISTS user_auth_providers")
            await self.database.execute("DROP TABLE IF EXISTS users")
            await self.database.commit()

            # Recreate tables with current schema
            await self._create_database_tables()

    async def _setup_login_providers(self, allow_self_registration: bool) -> None:
        """
        Set up available login providers based on configuration.

        :param allow_self_registration: Whether to allow self-registration via OAuth.
        """
        # Always enable built-in provider
        builtin_config: LoginProviderConfig = {"allow_self_registration": False}
        self.login_providers["builtin"] = BuiltinLoginProvider(self.mass, "builtin", builtin_config)

        # Home Assistant OAuth provider
        # Automatically enabled if HA provider (plugin) is configured
        ha_provider = None
        for provider in self.mass.providers:
            if provider.domain == "hass" and provider.available:
                ha_provider = provider
                break

        if ha_provider:
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

    async def _sync_ha_oauth_provider(self) -> None:
        """
        Sync HA OAuth provider with HA provider availability (dynamic check).

        Adds the provider if HA is available, removes it if HA is not available.
        """
        # Find HA provider
        ha_provider = None
        for provider in self.mass.providers:
            if provider.domain == "hass" and provider.available:
                ha_provider = provider
                break

        if ha_provider:
            # HA provider exists and is available - ensure OAuth provider is registered
            if "homeassistant" not in self.login_providers:
                # Get allow_self_registration config
                allow_self_registration = bool(
                    self.webserver.config.get_value(CONF_AUTH_ALLOW_SELF_REGISTRATION, True)
                )

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
                    "Home Assistant OAuth provider dynamically enabled (using URL: %s)",
                    ha_url,
                )
        # HA provider not available - remove OAuth provider if present
        elif "homeassistant" in self.login_providers:
            del self.login_providers["homeassistant"]
            self.logger.info("Home Assistant OAuth provider removed (HA provider not available)")

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

        # Implement sliding expiration for short-lived tokens
        is_long_lived = bool(token_row["is_long_lived"])
        now = utc()
        updates = {"last_used_at": now.isoformat()}

        if not is_long_lived and token_row["expires_at"]:
            # Short-lived token: extend expiration on each use (sliding window)
            new_expires_at = now + timedelta(days=TOKEN_SHORT_LIVED_EXPIRATION)
            updates["expires_at"] = new_expires_at.isoformat()

        # Update last used timestamp and potentially expiration
        await self.database.update(
            "auth_tokens",
            {"token_id": token_row["token_id"]},
            updates,
        )

        # Get user
        return await self.get_user(token_row["user_id"])

    async def get_token_id_from_token(self, token: str) -> str | None:
        """
        Get token_id from a token string (for tracking revocation).

        :param token: The access token.
        :return: The token_id or None if token not found.
        """
        # Hash the token to look it up
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        # Find token in database
        token_row = await self.database.get_row("auth_tokens", {"token_hash": token_hash})
        if not token_row:
            return None

        return str(token_row["token_id"])

    @api_command("auth/user", required_role="admin")
    async def get_user(self, user_id: str) -> User | None:
        """
        Get user by ID (admin only).

        :param user_id: The user ID.
        :return: User object or None if not found.
        """
        user_row = await self.database.get_row("users", {"user_id": user_id})
        if not user_row or not user_row["enabled"]:
            return None

        # Convert Row to dict for easier handling of optional fields
        user_dict = dict(user_row)

        # Parse preferences from JSON
        preferences = {}
        if prefs_json := user_dict.get("preferences"):
            try:
                preferences = json_loads(prefs_json)
            except Exception:
                self.logger.warning("Failed to parse preferences for user %s", user_id)

        return User(
            user_id=user_dict["user_id"],
            username=user_dict["username"],
            role=UserRole(user_dict["role"]),
            enabled=bool(user_dict["enabled"]),
            created_at=datetime.fromisoformat(user_dict["created_at"]),
            display_name=user_dict.get("display_name"),
            avatar_url=user_dict.get("avatar_url"),
            preferences=preferences,
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
        preferences: dict[str, Any] | None = None,
    ) -> User:
        """
        Create a new user.

        :param username: The username.
        :param role: The user role (default: USER).
        :param display_name: Optional display name.
        :param avatar_url: Optional avatar URL.
        :param preferences: Optional user preferences dict.
        """
        user_id = secrets.token_urlsafe(32)
        created_at = utc()
        if preferences is None:
            preferences = {}

        user_data = {
            "user_id": user_id,
            "username": username,
            "role": role.value,
            "enabled": True,
            "created_at": created_at.isoformat(),
            "display_name": display_name,
            "avatar_url": avatar_url,
            "preferences": json_dumps(preferences),
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
            preferences=preferences,
        )

    async def get_homeassistant_system_user(self) -> User:
        """
        Get or create the Home Assistant system user.

        This is a special system user created automatically for Home Assistant integration.
        It bypasses normal authentication but is restricted to the ingress webserver.

        :return: The Home Assistant system user.
        """
        username = HOMEASSISTANT_SYSTEM_USER
        display_name = "Home Assistant Integration"
        role = UserRole.USER

        # Try to find existing user by username
        user_row = await self.database.get_row("users", {"username": username})
        if user_row:
            # Use get_user to ensure preferences are parsed correctly
            user = await self.get_user(user_row["user_id"])
            assert user is not None  # User exists in DB, so get_user must return it
            return user

        # Create new system user
        user = await self.create_user(
            username=username,
            role=role,
            display_name=display_name,
        )
        self.logger.debug("Created Home Assistant system user: %s (role: %s)", username, role.value)
        return user

    async def get_homeassistant_system_user_token(self) -> str:
        """
        Get or create an auth token for the Home Assistant system user.

        This method ensures only one active token exists for the HA integration.
        If an old token exists, it is deleted and a new one is created.
        The token auto-renews on use (expires after 30 days of inactivity).

        :return: Authentication token for the Home Assistant system user.
        """
        token_name = "Home Assistant Integration"

        # Get the system user
        system_user = await self.get_homeassistant_system_user()

        # Delete any existing tokens with this name to avoid accumulation
        # We can't retrieve the plain token from the hash, so we always create a new one
        existing_tokens = await self.database.get_rows(
            "auth_tokens",
            {"user_id": system_user.user_id, "name": token_name},
        )
        for token_row in existing_tokens:
            await self.database.delete("auth_tokens", {"token_id": token_row["token_id"]})

        # Create a new token for the system user
        return await self.create_token(
            user=system_user,
            name=token_name,
            is_long_lived=False,
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

    async def update_user(
        self,
        user: User,
        username: str | None = None,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> User:
        """
        Update a user's profile information.

        :param user: The user to update.
        :param username: New username (optional).
        :param display_name: New display name (optional).
        :param avatar_url: New avatar URL (optional).
        """
        updates = {}
        if username is not None:
            updates["username"] = username
        if display_name is not None:
            updates["display_name"] = display_name
        if avatar_url is not None:
            updates["avatar_url"] = avatar_url

        if updates:
            await self.database.update("users", {"user_id": user.user_id}, updates)

        # Return updated user
        updated_user = await self.get_user(user.user_id)
        assert updated_user is not None  # User exists, so get_user must return it
        return updated_user

    async def update_user_preferences(
        self,
        user: User,
        preferences: dict[str, Any],
    ) -> User:
        """
        Update a user's preferences.

        :param user: The user to update.
        :param preferences: New preferences dict (completely replaces existing preferences).
        """
        # Verify user exists
        current_user = await self.get_user(user.user_id)
        if not current_user:
            raise ValueError(f"User {user.user_id} not found")

        # Update database with new preferences (complete replacement)
        await self.database.update(
            "users",
            {"user_id": user.user_id},
            {"preferences": json_dumps(preferences)},
        )

        # Return updated user
        updated_user = await self.get_user(user.user_id)
        assert updated_user is not None  # User exists, so get_user must return it
        return updated_user

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

    async def create_token(self, user: User, name: str, is_long_lived: bool = False) -> str:
        """
        Create a new access token for a user.

        :param user: The user to create the token for.
        :param name: A name/description for the token (e.g., device name).
        :param is_long_lived: Whether this is a long-lived token (default: False).
            Short-lived tokens (False): Auto-renewing on use, expire after 30 days of inactivity.
            Long-lived tokens (True): No auto-renewal, expire after 10 years.
        """
        # Generate token
        token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        # Calculate expiration based on token type
        created_at = utc()
        if is_long_lived:
            # Long-lived tokens expire after 10 years (no auto-renewal)
            expires_at = created_at + timedelta(days=TOKEN_LONG_LIVED_EXPIRATION)
        else:
            # Short-lived tokens expire after 30 days (with auto-renewal on use)
            expires_at = created_at + timedelta(days=TOKEN_SHORT_LIVED_EXPIRATION)

        # Store token
        token_data = {
            "token_id": secrets.token_urlsafe(32),
            "user_id": user.user_id,
            "token_hash": token_hash,
            "name": name,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "is_long_lived": 1 if is_long_lived else 0,
        }
        await self.database.insert("auth_tokens", token_data)

        return token

    @api_command("auth/token/revoke")
    async def revoke_token(self, token_id: str) -> None:
        """
        Revoke an auth token.

        :param token_id: The token ID to revoke.
        """
        user = get_current_user()
        if not user:
            raise AuthenticationRequired("Not authenticated")

        token_row = await self.database.get_row("auth_tokens", {"token_id": token_id})
        if not token_row:
            raise InvalidDataError("Token not found")

        # Check permissions - users can only revoke their own tokens unless admin
        if token_row["user_id"] != user.user_id and user.role != UserRole.ADMIN:
            raise InsufficientPermissions("You can only revoke your own tokens")

        await self.database.delete("auth_tokens", {"token_id": token_id})

        # Disconnect any WebSocket connections using this token
        self.webserver.disconnect_websockets_for_token(token_id)

    @api_command("auth/tokens")
    async def get_user_tokens(self, user_id: str | None = None) -> list[AuthToken]:
        """
        Get current user's auth tokens or another user's tokens (admin only).

        :param user_id: Optional user ID to get tokens for (admin only).
        :return: List of auth tokens.
        """
        current_user = get_current_user()
        if not current_user:
            return []

        # If user_id is provided and different from current user, require admin
        if user_id and user_id != current_user.user_id:
            if current_user.role != UserRole.ADMIN:
                return []
            target_user = await self.get_user(user_id)
            if not target_user:
                return []
        else:
            target_user = current_user

        token_rows = await self.database.get_rows(
            "auth_tokens", {"user_id": target_user.user_id}, limit=100
        )
        return [AuthToken.from_dict(dict(row)) for row in token_rows]

    @api_command("auth/users", required_role="admin")
    async def list_users(self) -> list[User]:
        """
        Get all users (admin only).

        System users are excluded from the list.

        :return: List of user objects.
        """
        user_rows = await self.database.get_rows("users", limit=1000)
        users = []
        for row in user_rows:
            row_dict = dict(row)

            # Skip system users
            if row_dict["username"] == HOMEASSISTANT_SYSTEM_USER:
                continue

            # Parse preferences
            preferences = {}
            if prefs_json := row_dict.get("preferences"):
                try:
                    preferences = json_loads(prefs_json)
                except Exception:
                    self.logger.warning(
                        "Failed to parse preferences for user %s", row_dict["user_id"]
                    )

            users.append(
                User(
                    user_id=row_dict["user_id"],
                    username=row_dict["username"],
                    role=UserRole(row_dict["role"]),
                    enabled=bool(row_dict["enabled"]),
                    created_at=datetime.fromisoformat(row_dict["created_at"]),
                    display_name=row_dict.get("display_name"),
                    avatar_url=row_dict.get("avatar_url"),
                    preferences=preferences,
                )
            )
        return users

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

    @api_command("auth/user/enable", required_role="admin")
    async def enable_user(self, user_id: str) -> None:
        """
        Enable user account (admin only).

        :param user_id: The user ID.
        """
        await self.database.update(
            "users",
            {"user_id": user_id},
            {"enabled": 1},
        )

    @api_command("auth/user/disable", required_role="admin")
    async def disable_user(self, user_id: str) -> None:
        """
        Disable user account (admin only).

        :param user_id: The user ID.
        """
        admin_user = get_current_user()
        if not admin_user:
            raise AuthenticationRequired("Not authenticated")

        # Cannot disable yourself
        if user_id == admin_user.user_id:
            raise InvalidDataError("Cannot disable your own account")

        await self.database.update(
            "users",
            {"user_id": user_id},
            {"enabled": 0},
        )

        # Disconnect all WebSocket connections for this user
        self.webserver.disconnect_websockets_for_user(user_id)

    async def get_login_providers(self) -> list[dict[str, Any]]:
        """Get list of available login providers (dynamically checks for HA provider)."""
        # Sync HA OAuth provider with HA provider availability
        await self._sync_ha_oauth_provider()

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

    async def get_authorization_url(
        self, provider_id: str, return_url: str | None = None
    ) -> str | None:
        """
        Get OAuth authorization URL for a provider.

        :param provider_id: The provider ID.
        :param return_url: Optional URL to redirect to after successful login.
        """
        provider = self.login_providers.get(provider_id)
        if not provider or not provider.requires_redirect:
            return None

        # Build callback redirect_uri
        redirect_uri = f"{self.webserver.base_url}/auth/callback?provider_id={provider_id}"
        return await provider.get_authorization_url(redirect_uri, return_url)

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

    @api_command("auth/token/create")
    async def create_long_lived_token(self, name: str, user_id: str | None = None) -> str:
        """
        Create a new long-lived access token for current user or another user (admin only).

        Long-lived tokens are intended for external integrations and API access.
        They expire after 10 years and do NOT auto-renew on use.

        Short-lived tokens (for regular user sessions) are only created during login
        and auto-renew on each use (sliding 30-day expiration window).

        :param name: The name/description for the token (e.g., "Home Assistant", "Mobile App").
        :param user_id: Optional user ID to create token for (admin only).
        :return: The created token string.
        """
        current_user = get_current_user()
        if not current_user:
            raise AuthenticationRequired("Not authenticated")

        # If user_id is provided and different from current user, require admin
        if user_id and user_id != current_user.user_id:
            if current_user.role != UserRole.ADMIN:
                raise InsufficientPermissions(
                    "Admin access required to create tokens for other users"
                )
            target_user = await self.get_user(user_id)
            if not target_user:
                raise InvalidDataError("User not found")
        else:
            target_user = current_user

        # Create a long-lived token (only long-lived tokens can be created via this command)
        token = await self.create_token(target_user, name, is_long_lived=True)
        self.logger.info("Created long-lived token '%s' for user '%s'", name, target_user.username)
        return token

    @api_command("auth/user/create", required_role="admin")
    async def create_user_with_api(
        self,
        username: str,
        password: str,
        role: str = "user",
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> User:
        """
        Create a new user with built-in authentication (admin only).

        :param username: The username (minimum 3 characters).
        :param password: The password (minimum 8 characters).
        :param role: User role - "admin" or "user" (default: "user").
        :param display_name: Optional display name.
        :param avatar_url: Optional avatar URL.
        :return: Created user object.
        """
        # Validation
        if not username or len(username) < 3:
            raise InvalidDataError("Username must be at least 3 characters")

        if not password or len(password) < 8:
            raise InvalidDataError("Password must be at least 8 characters")

        # Validate role
        try:
            user_role = UserRole(role)
        except ValueError as err:
            raise InvalidDataError("Invalid role. Must be 'admin' or 'user'") from err

        # Get built-in provider
        builtin_provider = self.login_providers.get("builtin")
        if not builtin_provider or not isinstance(builtin_provider, BuiltinLoginProvider):
            raise InvalidDataError("Built-in auth provider not available")

        # Create user with password
        user = await builtin_provider.create_user_with_password(username, password, role=user_role)

        # Update optional fields if provided
        if display_name or avatar_url:
            updated_user = await self.update_user(
                user, display_name=display_name, avatar_url=avatar_url
            )
            if updated_user:
                user = updated_user

        self.logger.info("User created by admin: %s (role: %s)", username, role)
        return user

    @api_command("auth/user/delete", required_role="admin")
    async def delete_user(self, user_id: str) -> None:
        """
        Delete user account (admin only).

        :param user_id: The user ID.
        """
        admin_user = get_current_user()
        if not admin_user:
            raise AuthenticationRequired("Not authenticated")

        # Don't allow deleting yourself
        if user_id == admin_user.user_id:
            raise InvalidDataError("Cannot delete your own account")

        # Delete user from database
        await self.database.delete("users", {"user_id": user_id})
        await self.database.commit()

        # Disconnect all WebSocket connections for this user
        self.webserver.disconnect_websockets_for_user(user_id)

    @api_command("auth/me")
    async def get_current_user_info(self) -> User:
        """Get current authenticated user information."""
        current_user_obj = get_current_user()
        if not current_user_obj:
            raise AuthenticationRequired("Not authenticated")
        return current_user_obj

    async def _update_profile_password(
        self,
        target_user: User,
        password: str,
        old_password: str | None,
        is_admin_update: bool,
        current_user: User,
    ) -> None:
        """Update user password (helper method)."""
        if len(password) < 8:
            raise InvalidDataError("Password must be at least 8 characters")

        builtin_provider = self.login_providers.get("builtin")
        if not builtin_provider or not isinstance(builtin_provider, BuiltinLoginProvider):
            raise InvalidDataError("Built-in auth not available")

        if is_admin_update:
            # Admin can reset password without old password
            await builtin_provider.reset_password(target_user, password)
            self.logger.info(
                "Password reset for user %s by admin %s",
                target_user.username,
                current_user.username,
            )
        else:
            # User updating own password - requires old password verification
            if not old_password:
                raise InvalidDataError("old_password is required to change your own password")

            # Verify old password and change to new one
            success = await builtin_provider.change_password(target_user, old_password, password)
            if not success:
                raise AuthenticationFailed("Invalid current password")

            self.logger.info("Password changed for user %s", target_user.username)

    @api_command("auth/user/update")
    async def update_user_profile(
        self,
        user_id: str | None = None,
        username: str | None = None,
        display_name: str | None = None,
        avatar_url: str | None = None,
        password: str | None = None,
        old_password: str | None = None,
        role: str | None = None,
        preferences: dict[str, Any] | None = None,
    ) -> User:
        """
        Update user profile information.

        Users can update their own profile. Admins can update any user including role and password.

        :param user_id: User ID to update (optional, defaults to current user).
        :param username: New username (optional).
        :param display_name: New display name (optional).
        :param avatar_url: New avatar URL (optional).
        :param password: New password (optional, minimum 8 characters).
        :param old_password: Current password (required when user updates own password).
        :param role: New role - "admin" or "user" (optional, admin only).
        :param preferences: User preferences dict (completely replaces existing, optional).
        :return: Updated user object.
        """
        current_user_obj = get_current_user()
        if not current_user_obj:
            raise AuthenticationRequired("Not authenticated")

        # Determine target user
        if user_id and user_id != current_user_obj.user_id:
            # Updating another user - requires admin
            if current_user_obj.role != UserRole.ADMIN:
                raise InsufficientPermissions("Admin access required")
            target_user = await self.get_user(user_id)
            if not target_user:
                raise InvalidDataError("User not found")
            is_admin_update = True
        else:
            # Updating own profile
            target_user = current_user_obj
            is_admin_update = False

        # Update role (admin only)
        if role:
            if not is_admin_update:
                raise InsufficientPermissions("Only admins can update user roles")

            try:
                new_role = UserRole(role)
            except ValueError as err:
                raise InvalidDataError("Invalid role. Must be 'admin' or 'user'") from err

            success = await self.update_user_role(target_user.user_id, new_role, current_user_obj)
            if not success:
                raise InvalidDataError("Failed to update role")

            # Refresh target user to get updated role
            refreshed_user = await self.get_user(target_user.user_id)
            if not refreshed_user:
                raise InvalidDataError("Failed to refresh user after role update")
            target_user = refreshed_user

        # Update basic profile fields
        if username or display_name or avatar_url:
            updated_user = await self.update_user(
                target_user,
                username=username,
                display_name=display_name,
                avatar_url=avatar_url,
            )
            if not updated_user:
                raise InvalidDataError("Failed to update user profile")
            target_user = updated_user

        # Update preferences if provided
        if preferences is not None:
            target_user = await self.update_user_preferences(target_user, preferences)

        # Update password if provided
        if password:
            await self._update_profile_password(
                target_user, password, old_password, is_admin_update, current_user_obj
            )

        return target_user

    @api_command("auth/logout")
    async def logout(self) -> None:
        """Logout current user by revoking the current token."""
        user = get_current_user()
        if not user:
            raise AuthenticationRequired("Not authenticated")

        # Get current token from context
        token = get_current_token()
        if not token:
            raise InvalidDataError("No token in context")

        # Find and revoke the token
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        token_row = await self.database.get_row("auth_tokens", {"token_hash": token_hash})
        if token_row:
            await self.database.delete("auth_tokens", {"token_id": token_row["token_id"]})

            # Disconnect any WebSocket connections using this token
            self.webserver.disconnect_websockets_for_token(token_row["token_id"])

    @api_command("auth/user/providers")
    async def get_my_providers(self) -> list[dict[str, Any]]:
        """
        Get current user's linked authentication providers.

        :return: List of provider links.
        """
        user = get_current_user()
        if not user:
            return []

        # Get provider links from database
        rows = await self.database.get_rows("user_auth_providers", {"user_id": user.user_id})
        providers = [UserAuthProvider.from_dict(dict(row)) for row in rows]
        return [p.to_dict() for p in providers]

    @api_command("auth/user/unlink_provider", required_role="admin")
    async def unlink_provider(self, user_id: str, provider_type: str) -> bool:
        """
        Unlink authentication provider from user (admin only).

        :param user_id: The user ID.
        :param provider_type: Provider type to unlink.
        :return: True if successful.
        """
        await self.database.delete(
            "user_auth_providers", {"user_id": user_id, "provider_type": provider_type}
        )
        await self.database.commit()
        return True
