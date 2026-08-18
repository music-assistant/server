"""Authentication manager for Music Assistant webserver."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
from sqlite3 import IntegrityError, OperationalError
from typing import TYPE_CHECKING, Any, cast

import jwt as pyjwt
from music_assistant_models.auth import (
    AuthProviderType,
    AuthToken,
    Scope,
    User,
    UserAuthProvider,
    UserRole,
)
from music_assistant_models.errors import (
    AuthenticationRequired,
    InsufficientPermissions,
    InvalidDataError,
)

from music_assistant.constants import (
    CONF_PLAYERS,
    CONF_PROVIDERS,
    DB_TABLE_PLAYLOG,
    HOMEASSISTANT_SYSTEM_USER,
    MASS_LOGGER_NAME,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    ROLE_SCOPES,
    get_current_client_id,
    get_current_peer_address,
    get_current_token,
    get_current_user,
    has_scope,
)
from music_assistant.controllers.webserver.helpers.auth_providers import (
    AuthResult,
    BuiltinLoginProvider,
    HomeAssistantOAuthProvider,
    HomeAssistantProviderConfig,
    LoginProvider,
    LoginRateLimiter,
    normalize_username,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.datetime import utc
from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.helpers.jwt_auth import JWTHelper

if TYPE_CHECKING:
    from music_assistant.controllers.webserver import WebserverController
    from music_assistant.providers.hass import HomeAssistantProvider

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.auth")

# Database schema version
DB_SCHEMA_VERSION = 5

# Token expiration constants (in days)
TOKEN_SHORT_LIVED_EXPIRATION = 30  # Short-lived tokens (auto-renewing on use)
TOKEN_LONG_LIVED_EXPIRATION = 365  # Long-lived tokens (1 year, no auto-renewal)
# Max days a sliding short-lived session may live from creation before re-auth.
TOKEN_ABSOLUTE_MAX_EXPIRATION = 90
TOKEN_GUEST_EXPIRATION = 1  # Guest sessions: short fixed lifetime, no renewal
# Days before the absolute cap at which the HA integration token is rotated
HA_TOKEN_ROTATION_MARGIN = 7
# Minimum age of a token's stored last_used_at before token activity is persisted again
TOKEN_ACTIVITY_PERSIST_INTERVAL = timedelta(hours=1)

HA_TOKEN_SETTING_KEY = "ha_integration_token"
HA_TOKEN_NAME = "Home Assistant Integration"

# Join code constants (short codes for QR/link-based login)
JOIN_CODE_LENGTH = 12
JOIN_CODE_CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # No I/O/0/1 for readability
JOIN_CODE_DEFAULT_EXPIRY_HOURS = 8
# Failed exchanges are throttled per calling websocket connection, so one guest fumbling a
# stale QR code cannot lock out every other guest at a party. Callers that reach the API
# without a connection identity (the JSON RPC endpoint, in-process callers) share one bucket.
JOIN_CODE_ANONYMOUS_RATE_LIMIT_KEY = "no-connection"
# Second, server-wide bucket that backstops the per-connection buckets, since a client can
# start a new connection (and thus a new bucket) at will. The join code itself is what makes
# guessing infeasible (12 chars over a 32 symbol alphabet is ~2^60, valid for hours), so this
# ceiling is deliberately far above any plausible party-scale burst of legitimate failures.
JOIN_CODE_GLOBAL_RATE_LIMIT_KEY = "all-connections"
JOIN_CODE_GLOBAL_FAILURE_CEILING = 1000
JOIN_CODE_GLOBAL_COOLDOWN_SECONDS = 60


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
        self._has_users: bool = False
        self.jwt_helper: JWTHelper = None  # type: ignore[assignment]
        self._join_code_rate_limiter = LoginRateLimiter(subject="client")
        self._join_code_global_rate_limiter = LoginRateLimiter(
            delay_tiers=((JOIN_CODE_GLOBAL_FAILURE_CEILING, JOIN_CODE_GLOBAL_COOLDOWN_SECONDS),),
            warn_threshold=JOIN_CODE_GLOBAL_FAILURE_CEILING,
            alert_threshold=JOIN_CODE_GLOBAL_FAILURE_CEILING * 2,
            subject="join_codes",
        )
        # Stops concurrent exchanges from passing the rate limit check before failures land
        self._join_code_exchange_lock = asyncio.Lock()
        # Serialises the read-modify-write of the user access filters
        self._user_filter_lock = asyncio.Lock()
        self._access_revoked_callbacks: list[Callable[[User], None]] = []

    async def setup(self) -> None:
        """Initialize the authentication manager."""
        # Setup database
        db_path = self.mass.storage_path + "/auth.db"
        self.database = DatabaseConnection(db_path)
        await self.database.setup()

        # Create database schema and handle migrations
        await self._setup_database()

        # Initialize JWT helper with secret key
        jwt_secret = await self._get_or_create_jwt_secret()
        self.jwt_helper = JWTHelper(jwt_secret)

        # Setup login providers
        await self._setup_login_providers()

        self._has_users = await self._has_non_system_users()

        # migrate the Home Assistant system user of pre-existing installs to the service role
        await self._migrate_system_user_role()

        # repair filters that were left pointing at removed providers/players
        await self._prune_stale_user_filters()

        self._schedule_join_code_cleanup()

        self.logger.info(
            "Authentication manager initialized (providers=%d)", len(self.login_providers)
        )

    async def close(self) -> None:
        """Cleanup on exit."""
        if self.database:
            await self.database.close()

    @property
    def has_users(self) -> bool:
        """Check if any users exist in the system."""
        return self._has_users

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
        Authenticate a user with an access token (JWT or legacy).

        Supports both JWT tokens and legacy hash-based tokens for backward compatibility.

        :param token: The access token (JWT or legacy hash token).
        """
        # Try to decode as JWT first
        try:
            payload = self.jwt_helper.decode_token(token, verify_exp=True)
            token_id = payload.get("jti")
            token_user_id = payload.get("sub")

            if not token_id or not token_user_id:
                return None

            token_row = await self.database.get_row("auth_tokens", {"token_id": token_id})
            if not token_row:
                return None

            # Database is source of truth for token metadata, not the (immutable) JWT payload.
            # A payload/row mismatch means a tampered or stale token: reject rather than trust it.
            if token_user_id != token_row["user_id"]:
                return None
            is_long_lived = bool(token_row["is_long_lived"])

            # Database expiration is source of truth
            if token_row["expires_at"]:
                db_expires_at = datetime.fromisoformat(token_row["expires_at"])
                if utc() > db_expires_at:
                    await self.database.delete("auth_tokens", {"token_id": token_id})
                    return None

            user = await self.get_user(token_row["user_id"])
            if not user:
                return None

            updates = await self._refresh_token_expiration(token_row, user, is_long_lived)
            if updates is None:
                return None
            if updates:
                await self.database.update("auth_tokens", {"token_id": token_id}, updates)

            return user

        except pyjwt.ExpiredSignatureError:
            if token_id := self.jwt_helper.get_token_id(token):
                await self.database.delete("auth_tokens", {"token_id": token_id})
            return None
        except pyjwt.InvalidTokenError:
            self.logger.debug("Token is not a valid JWT, trying legacy hash lookup")
        except Exception as err:
            self.logger.debug("Error decoding JWT token: %s, trying legacy hash lookup", err)

        # Fallback to legacy hash-based token lookup
        token_hash = hashlib.sha256(token.encode()).hexdigest()
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

        user = await self.get_user(token_row["user_id"])
        if not user:
            return None

        is_long_lived = bool(token_row["is_long_lived"])
        legacy_updates = await self._refresh_token_expiration(token_row, user, is_long_lived)
        if legacy_updates is None:
            return None
        if legacy_updates:
            await self.database.update(
                "auth_tokens", {"token_id": token_row["token_id"]}, legacy_updates
            )

        return user

    async def get_token_id_from_token(self, token: str) -> str | None:
        """
        Get token_id from a token string (for tracking revocation).

        :param token: The access token (JWT or legacy hash token).
        :return: The token_id or None if token not found.
        """
        # Try to extract from JWT first
        if token_id := self.jwt_helper.get_token_id(token):
            return token_id

        # Fallback: Hash-based lookup for legacy tokens
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        token_row = await self.database.get_row("auth_tokens", {"token_hash": token_hash})
        if not token_row:
            return None
        return str(token_row["token_id"])

    @api_command("auth/user", required_scope=Scope.USERS_READ)
    async def get_user(self, user_id: str) -> User | None:
        """
        Get user by ID (requires the users.read scope).

        :param user_id: The user ID.
        :return: User object or None if not found.
        """
        user_row = await self.database.get_row("users", {"user_id": user_id})
        if not user_row or not user_row["enabled"]:
            return None

        return User(
            user_id=user_row["user_id"],
            username=user_row["username"],
            role=user_row["role"],
            enabled=bool(user_row["enabled"]),
            created_at=datetime.fromisoformat(user_row["created_at"]),
            display_name=user_row["display_name"],
            avatar_url=user_row["avatar_url"],
            preferences=json_loads(user_row["preferences"]),
            player_filter=json_loads(user_row["player_filter"]),
            provider_filter=json_loads(user_row["provider_filter"]),
        )

    async def get_user_by_username(self, username: str) -> User | None:
        """
        Get user by username.

        :param username: The username.
        :return: User object or None if not found.
        """
        username = normalize_username(username)

        user_row = await self.database.get_row("users", {"username": username})
        if not user_row:
            return None

        return await self.get_user(user_row["user_id"])

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
        player_filter: list[str] | None = None,
        provider_filter: list[str] | None = None,
    ) -> User:
        """
        Create a new user.

        :param username: The username.
        :param role: The user role (default: USER).
        :param display_name: Optional display name.
        :param avatar_url: Optional avatar URL.
        :param preferences: Optional user preferences dict.
        :param player_filter: Optional list of player IDs user has access to.
        :param provider_filter: Optional list of provider instance IDs user has access to.
        """
        normalized_username = normalize_username(username)

        # Check if this is the first non-system user
        is_first_user = not await self._has_non_system_users()

        user_id = secrets.token_urlsafe(32)
        created_at = utc()
        if preferences is None:
            preferences = {}
        if player_filter is None:
            player_filter = []
        if provider_filter is None:
            provider_filter = []

        user_data = {
            "user_id": user_id,
            "username": normalized_username,
            "role": role.value,
            "enabled": True,
            "created_at": created_at.isoformat(),
            "display_name": display_name,
            "avatar_url": avatar_url,
            "preferences": json_dumps(preferences),
            "player_filter": json_dumps(player_filter),
            "provider_filter": json_dumps(provider_filter),
        }

        await self.database.insert("users", user_data)

        user = User(
            user_id=user_id,
            username=normalized_username,
            role=role,
            enabled=True,
            created_at=created_at,
            display_name=display_name,
            avatar_url=avatar_url,
            preferences=preferences,
            player_filter=player_filter,
            provider_filter=provider_filter,
        )

        # If this is the first non-system user, migrate playlog entries to them
        if is_first_user and normalized_username != HOMEASSISTANT_SYSTEM_USER:
            self._has_users = True
            await self._migrate_playlog_to_first_user(user_id)

        return user

    async def get_homeassistant_system_user(self) -> User:
        """
        Get or create the Home Assistant system user.

        This is a special system user created automatically for Home Assistant integration.
        It bypasses normal authentication but is restricted to the ingress webserver.

        :return: The Home Assistant system user.
        """
        username = HOMEASSISTANT_SYSTEM_USER
        display_name = "Home Assistant Integration"
        role = UserRole.SERVICE

        normalized_username = normalize_username(username)

        # Try to find existing user by username
        user_row = await self.database.get_row("users", {"username": normalized_username})
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
        Get the auth token to announce to the Home Assistant integration.

        Returns the same (still valid) token on repeated calls so re-announcing it via
        Supervisor discovery is idempotent for the HA integration. A replacement is only
        minted when the current token is missing, expired or revoked, or shortly before
        it reaches its absolute lifetime cap - allowing seamless rotation as HA reloads
        with the newly announced token while the old one is still accepted.

        :return: Authentication token for the Home Assistant system user.
        """
        system_user = await self.get_homeassistant_system_user()

        # Keep the plain token in settings for re-announcing; the jwt_secret next to it can mint any token anyway
        if token_row := await self.database.get_row("settings", {"key": HA_TOKEN_SETTING_KEY}):
            token = str(token_row["value"])
            if await self._can_reuse_ha_integration_token(token, system_user):
                return token

        # A superseded token stays valid until expiry, so HA keeps working until it reloads
        token = await self.create_token(
            user=system_user,
            name=HA_TOKEN_NAME,
            is_long_lived=False,
        )
        await self.database.insert_or_replace(
            "settings",
            {"key": HA_TOKEN_SETTING_KEY, "value": token, "type": "string"},
        )
        now = utc()
        for old_row in await self.database.get_rows(
            "auth_tokens", {"user_id": system_user.user_id, "name": HA_TOKEN_NAME}
        ):
            if old_row["expires_at"] and datetime.fromisoformat(old_row["expires_at"]) <= now:
                await self.database.delete("auth_tokens", {"token_id": old_row["token_id"]})
        await self.database.commit()
        return token

    async def link_user_to_provider(
        self,
        user: User,
        provider_type: AuthProviderType,
        provider_user_id: str,
    ) -> UserAuthProvider:
        """
        Link a user to an authentication provider.

        If a link already exists for this provider/provider_user_id, returns the existing link.

        :param user: The user to link.
        :param provider_type: The provider type.
        :param provider_user_id: The user ID from the provider (e.g., password hash, OAuth ID).
        """
        # Check if a link already exists for this provider/provider_user_id
        existing_link = await self.database.get_row(
            "user_auth_providers",
            {
                "provider_type": provider_type.value,
                "provider_user_id": provider_user_id,
            },
        )

        if existing_link:
            # Link already exists - return it
            return UserAuthProvider(
                link_id=existing_link["link_id"],
                user_id=existing_link["user_id"],
                provider_type=AuthProviderType(existing_link["provider_type"]),
                provider_user_id=existing_link["provider_user_id"],
                created_at=datetime.fromisoformat(existing_link["created_at"]),
            )

        # Create new link
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
            # Normalize username for case-insensitive authentication
            updates["username"] = normalize_username(username)
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
        Create a new JWT access token for a user.

        :param user: The user to create the token for.
        :param name: A name/description for the token (e.g., device name).
        :param is_long_lived: Whether this is a long-lived token (default: False).
            Short-lived tokens (False): Auto-renewing on use, expire after 30 days of inactivity,
            capped at an absolute maximum lifetime from creation (see TOKEN_ABSOLUTE_MAX_EXPIRATION).
            Tokens for guest users get a short fixed lifetime instead and never renew.
            Long-lived tokens (True): No auto-renewal, expire after 1 year.
        :return: JWT token string.
        """
        # Generate unique token ID
        token_id = secrets.token_urlsafe(32)

        # Calculate expiration based on token type
        created_at = utc()
        if is_long_lived:
            # Long-lived tokens expire after 1 year (no auto-renewal)
            expires_at = created_at + timedelta(days=TOKEN_LONG_LIVED_EXPIRATION)
            jwt_expires_at = expires_at
        elif user.role == UserRole.GUEST:
            expires_at = created_at + timedelta(days=TOKEN_GUEST_EXPIRATION)
            jwt_expires_at = expires_at
        else:
            # Short-lived tokens expire after 30 days (with auto-renewal on use)
            expires_at = created_at + timedelta(days=TOKEN_SHORT_LIVED_EXPIRATION)
            # The exp claim must carry the absolute cap, or it would cut off sliding renewals
            jwt_expires_at = created_at + timedelta(days=TOKEN_ABSOLUTE_MAX_EXPIRATION)

        # Generate JWT token
        token = self.jwt_helper.encode_token(
            user=user,
            token_id=token_id,
            token_name=name,
            expires_at=jwt_expires_at,
            is_long_lived=is_long_lived,
        )

        # Store token hash in database for revocation checking
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        token_data = {
            "token_id": token_id,
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

        # Check permissions - users can only revoke their own tokens
        # unless they hold the users.manage scope
        if token_row["user_id"] != user.user_id and not has_scope(user, Scope.USERS_MANAGE):
            raise InsufficientPermissions("You can only revoke your own tokens")

        await self.database.delete("auth_tokens", {"token_id": token_id})

        # Disconnect any WebSocket connections using this token
        self.webserver.disconnect_websockets_for_token(token_id)

        self.logger.info(
            "Token revoked by user '%s' (token_id=%s)",
            user.username,
            token_id,
        )

    def subscribe_user_access_revoked(self, callback: Callable[[User], None]) -> Callable[[], None]:
        """
        Subscribe to a user's access being withdrawn.

        Fires on deliberate access withdrawal: bulk token revocation
        (revoke_tokens_for_user), account disable, and account deletion. Revoking a
        single token (e.g. a logout) does not fire it, so credentials bound to the
        account survive a plain logout.

        :param callback: Called with the affected user.
        :return: Callable that removes the subscription.
        """
        self._access_revoked_callbacks.append(callback)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._access_revoked_callbacks.remove(callback)

        return _unsubscribe

    async def revoke_tokens_for_user(self, user: User) -> int:
        """
        Revoke all auth tokens for a user.

        This is an internal method for programmatic use (e.g., when disabling guest access).
        Unlike revoke_token(), this does not require an authenticated user context.

        :param user: The user whose tokens should be revoked.
        :return: Number of tokens revoked.
        """
        token_rows = await self.database.get_rows("auth_tokens", {"user_id": user.user_id})

        # Disconnect any WebSocket connections using these tokens
        for token_row in token_rows:
            self.webserver.disconnect_websockets_for_token(token_row["token_id"])

        if token_rows:
            # Delete all tokens in one go
            await self.database.execute(
                "DELETE FROM auth_tokens WHERE user_id = :user_id",
                {"user_id": user.user_id},
            )
            await self.database.commit()
            self.logger.info("Revoked %d token(s) for user '%s'", len(token_rows), user.username)

        # Notify even with no tokens left: subscribers may hold credentials tied to
        # this user's access that must be withdrawn regardless.
        self._notify_user_access_revoked(user)

        return len(token_rows)

    @api_command("auth/tokens")
    async def get_user_tokens(self, user_id: str | None = None) -> list[AuthToken]:
        """
        Get current user's auth tokens or another user's tokens (admin only).

        The last_used_at timestamp is persisted at most once per hour, so it may lag
        actual token usage by up to an hour.

        :param user_id: Optional user ID to get tokens for (admin only).
        :return: List of auth tokens.
        """
        current_user = get_current_user()
        if not current_user:
            return []

        # If user_id is provided and different from current user,
        # require the users.manage scope
        if user_id and user_id != current_user.user_id:
            if not has_scope(current_user, Scope.USERS_MANAGE):
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

    @api_command("auth/users", required_scope=Scope.USERS_READ)
    async def list_users(self) -> list[User]:
        """
        Get all users (requires the users.read scope).

        System users are excluded from the list.

        :return: List of user objects.
        """
        user_rows = await self.database.get_rows("users", limit=1000)
        users = []
        for row in user_rows:
            # Skip system users
            if row["username"] == HOMEASSISTANT_SYSTEM_USER:
                continue
            users.append(
                User(
                    user_id=row["user_id"],
                    username=row["username"],
                    role=row["role"],
                    enabled=bool(row["enabled"]),
                    created_at=datetime.fromisoformat(row["created_at"]),
                    display_name=row["display_name"],
                    avatar_url=row["avatar_url"],
                    preferences=json_loads(row["preferences"]),
                    player_filter=json_loads(row["player_filter"]),
                    provider_filter=json_loads(row["provider_filter"]),
                )
            )
        return users

    async def update_user_role(self, user_id: str, new_role: UserRole, admin_user: User) -> bool:
        """
        Update a user's role (requires the users.manage scope).

        :param user_id: The user ID to update.
        :param new_role: The new role to assign.
        :param admin_user: The user performing the action.
        """
        if not has_scope(admin_user, Scope.USERS_MANAGE):
            return False

        user_row = await self.database.get_row("users", {"user_id": user_id})
        if not user_row:
            return False

        old_role = user_row["role"]
        await self.database.update(
            "users",
            {"user_id": user_id},
            {"role": new_role.value},
        )
        self.logger.info(
            "User role changed: '%s' from '%s' to '%s' by admin '%s'",
            user_row["username"],
            old_role,
            new_role.value,
            admin_user.username,
        )
        return True

    @api_command("auth/user/enable", required_scope=Scope.USERS_MANAGE)
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
        self.logger.info("User account enabled (user_id=%s)", user_id)

    @api_command("auth/user/disable", required_scope=Scope.USERS_MANAGE)
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

        # Look up the user before disabling (get_user hides disabled accounts)
        user_row = await self.database.get_row("users", {"user_id": user_id})
        if not user_row:
            raise InvalidDataError("User not found")

        await self.database.update(
            "users",
            {"user_id": user_id},
            {"enabled": 0},
        )

        # Disconnect all WebSocket connections for this user
        self.webserver.disconnect_websockets_for_user(user_id)

        # A disabled account's tokens stop authenticating, so credentials bound to its
        # access must be withdrawn with them (they return on the next login after enable).
        self._notify_user_access_revoked(
            User(user_id=user_row["user_id"], username=user_row["username"], role=user_row["role"])
        )

        self.logger.info("User account disabled (user_id=%s)", user_id)

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

    @api_command("auth/login", authenticated=False)
    async def login(
        self,
        username: str | None = None,
        password: str | None = None,
        provider_id: str = "builtin",
        device_name: str | None = None,
        **extra_credentials: Any,
    ) -> dict[str, Any]:
        """
        Authenticate user with credentials via WebSocket.

        This command allows clients to authenticate over the WebSocket connection
        using username/password or other provider-specific credentials.

        :param username: Username for authentication (for builtin provider).
        :param password: Password for authentication (for builtin provider).
        :param provider_id: The login provider ID (defaults to "builtin").
        :param device_name: Optional device name for the token (e.g., "iPhone 15", "Desktop PC").
        :param extra_credentials: Additional provider-specific credentials.
        :return: Authentication result with access token if successful.
        """
        # Build credentials dict from parameters
        credentials: dict[str, Any] = {}
        if username is not None:
            credentials["username"] = username
        if password is not None:
            credentials["password"] = password
        credentials.update(extra_credentials)

        auth_result = await self.authenticate_with_credentials(provider_id, credentials)

        if not auth_result.success:
            self.logger.warning(
                "Login failed for username '%s' via provider '%s'",
                username or "<not provided>",
                provider_id,
            )
            return {
                "success": False,
                "error": auth_result.error or "Authentication failed",
            }

        if not auth_result.user:
            return {
                "success": False,
                "error": "Authentication failed: no user returned",
            }

        # Create short-lived access token with device name if provided
        token_name = device_name or f"WebSocket Session - {auth_result.user.username}"
        token = await self.create_token(
            auth_result.user,
            is_long_lived=False,
            name=token_name,
        )

        self.logger.info(
            "User '%s' logged in via provider '%s'",
            auth_result.user.username,
            provider_id,
        )

        return {
            "success": True,
            "access_token": token,
            "user": {
                "user_id": auth_result.user.user_id,
                "username": auth_result.user.username,
                "display_name": auth_result.user.display_name,
                "role": auth_result.user.role,
            },
        }

    @api_command("auth/providers", authenticated=False)
    async def get_providers(self) -> list[dict[str, Any]]:
        """
        Get list of available authentication providers.

        Returns information about all available login providers including
        whether they require OAuth redirect flow.
        """
        return await self.get_login_providers()

    @api_command("auth/authorization_url", authenticated=False)
    async def get_auth_url(
        self,
        provider_id: str,
        return_url: str | None = None,
    ) -> dict[str, str | None]:
        """
        Get OAuth authorization URL for authentication.

        For OAuth providers (like Home Assistant), this returns the URL that
        the user should visit in their browser to authorize the application.

        :param provider_id: The provider ID (e.g., "hass").
        :param return_url: URL to redirect to after OAuth completes.
        :return: Dictionary with authorization_url.
        """
        auth_url = await self.get_authorization_url(provider_id, return_url)
        if not auth_url:
            return {
                "authorization_url": None,
                "error": "Provider does not support OAuth or does not exist",
            }

        return {
            "authorization_url": auth_url,
        }

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
        They expire after 1 year and do NOT auto-renew on use.

        Short-lived tokens (for regular user sessions) are only created during login
        and auto-renew on each use (sliding 30-day expiration window).

        Long-lived tokens cannot be created for guest accounts.

        :param name: The name/description for the token (e.g., "Home Assistant", "Mobile App").
        :param user_id: Optional user ID to create token for (admin only).
        :return: The created token string.
        """
        current_user = get_current_user()
        if not current_user:
            raise AuthenticationRequired("Not authenticated")

        # If user_id is provided and different from current user,
        # require the users.manage scope
        if user_id and user_id != current_user.user_id:
            if not has_scope(current_user, Scope.USERS_MANAGE):
                raise InsufficientPermissions(
                    "The users.manage scope is required to create tokens for other users"
                )
            target_user = await self.get_user(user_id)
            if not target_user:
                raise InvalidDataError("User not found")
        else:
            target_user = current_user

        # Guest access is temporary by design, deny tokens that would outlive it
        if target_user.role == UserRole.GUEST:
            raise InsufficientPermissions("Long-lived tokens cannot be created for guest accounts")

        # Create a long-lived token (only long-lived tokens can be created via this command)
        token = await self.create_token(target_user, name, is_long_lived=True)
        self.logger.info("Created long-lived token '%s' for user '%s'", name, target_user.username)
        return token

    @api_command("auth/user/create", required_scope=Scope.USERS_MANAGE)
    async def create_user_with_api(
        self,
        username: str,
        password: str,
        role: str = "user",
        display_name: str | None = None,
        avatar_url: str | None = None,
        player_filter: list[str] | None = None,
        provider_filter: list[str] | None = None,
    ) -> User:
        """
        Create a new user with built-in authentication (admin only).

        :param username: The username (minimum 2 characters).
        :param password: The password (minimum 8 characters).
        :param role: User role - "admin" or "user" (default: "user").
        :param display_name: Optional display name.
        :param avatar_url: Optional avatar URL.
        :param player_filter: Optional list of player IDs user has access to.
        :param provider_filter: Optional list of provider instance IDs user has access to.
        :return: Created user object.
        """
        # Validation
        if not username or len(username) < 2:
            raise InvalidDataError("Username must be at least 2 characters")

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
        user = await builtin_provider.create_user_with_password(
            username,
            password,
            role=user_role,
            player_filter=player_filter,
            provider_filter=provider_filter,
        )

        # Update optional fields if provided
        if display_name or avatar_url:
            updated_user = await self.update_user(
                user, display_name=display_name, avatar_url=avatar_url
            )
            if updated_user:
                user = updated_user

        self.logger.info("User created by admin: %s (role: %s)", username, role)
        return user

    @api_command("auth/user/delete", required_scope=Scope.USERS_MANAGE)
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

        # Look up the username before deleting
        user_row = await self.database.get_row("users", {"user_id": user_id})
        if not user_row:
            raise InvalidDataError("User not found")

        # Delete user from database
        await self.database.delete("users", {"user_id": user_id})
        await self.database.commit()

        # Disconnect all WebSocket connections for this user
        self.webserver.disconnect_websockets_for_user(user_id)

        # Deletion cascades the user's tokens away, so it must announce the access
        # withdrawal itself for credentials bound to this user.
        self._notify_user_access_revoked(
            User(user_id=user_row["user_id"], username=user_row["username"], role=user_row["role"])
        )

        self.logger.info(
            "User '%s' deleted by admin '%s'",
            user_row["username"],
            admin_user.username,
        )

    @api_command("auth/me")
    async def get_current_user_info(self) -> User:
        """Get current authenticated user information."""
        current_user_obj = get_current_user()
        if not current_user_obj:
            raise AuthenticationRequired("Not authenticated")
        return current_user_obj

    @api_command("auth/scopes")
    async def get_role_scopes(self) -> dict[str, list[str]]:
        """Get the scopes granted to each of the builtin user roles."""
        return {
            str(role): sorted(str(scope) for scope in scopes)
            for role, scopes in ROLE_SCOPES.items()
        }

    async def update_user_filters(
        self,
        target_user: User,
        player_filter: list[str] | None,
        provider_filter: list[str] | None,
    ) -> User:
        """Update user player and provider filters (helper method)."""
        updates = {}
        if player_filter is not None:
            updates["player_filter"] = json_dumps(player_filter)
        if provider_filter is not None:
            updates["provider_filter"] = json_dumps(provider_filter)

        if updates:
            await self.database.update("users", {"user_id": target_user.user_id}, updates)
            # Refresh target user to get updated filters
            refreshed_user = await self.get_user(target_user.user_id)
            if not refreshed_user:
                raise InvalidDataError("Failed to refresh user after filter update")
            return refreshed_user
        return target_user

    async def remove_from_user_filters(
        self,
        provider_instance_ids: Collection[str] = (),
        player_ids: Collection[str] = (),
    ) -> None:
        """
        Remove the given providers and/or players from the access filters of all users.

        Call this when a provider or player is permanently removed, so no user is left with
        an access filter that points at something that no longer exists.

        :param provider_instance_ids: Instance IDs of the removed providers.
        :param player_ids: IDs of the removed players.
        """
        await self._rewrite_user_filters(
            keep_provider=(lambda x: x not in provider_instance_ids)
            if provider_instance_ids
            else None,
            keep_player=(lambda x: x not in player_ids) if player_ids else None,
        )

    async def replace_player_in_user_filters(
        self,
        old_player_id: str,
        new_player_id: str,
        removed_player_ids: Collection[str] = (),
    ) -> None:
        """
        Point the access filters of all users at the replacement of a removed player.

        Call this when a player is automatically replaced by another one, so a user that
        is restricted to the old player follows the replacement instead of silently
        ending up with access to every player.

        :param old_player_id: ID of the player that is replaced.
        :param new_player_id: ID of the player that takes its place.
        :param removed_player_ids: IDs of the players that are removed along with it.
        """
        await self._rewrite_user_filters(
            keep_provider=None,
            keep_player=(lambda x: x not in removed_player_ids) if removed_player_ids else None,
            map_player=lambda x: new_player_id if x == old_player_id else x,
        )

    @api_command("auth/user/update")
    async def update_user_profile(
        self,
        user_id: str | None = None,
        username: str | None = None,
        display_name: str | None = None,
        avatar_url: str | None = None,
        password: str | None = None,
        role: str | None = None,
        preferences: dict[str, Any] | None = None,
        player_filter: list[str] | None = None,
        provider_filter: list[str] | None = None,
    ) -> User:
        """
        Update user profile information.

        Users can update their own profile. Admins can update any user including role and password.

        :param user_id: User ID to update (optional, defaults to current user).
        :param username: New username (optional).
        :param display_name: New display name (optional).
        :param avatar_url: New avatar URL (optional).
        :param password: New password (optional, minimum 8 characters).
        :param role: New role - "admin" or "user" (optional, set by admin only).
        :param preferences: User preferences dict (completely replaces existing, optional).
        :param player_filter: List of player IDs user has access to (set by admin only, optional).
        :param provider_filter: List of provider instance IDs user has access to (set by admin only, optional).
        :return: Updated user object.
        """
        current_user_obj = get_current_user()
        if not current_user_obj:
            raise AuthenticationRequired("Not authenticated")

        # Determine target user
        may_manage_users = has_scope(current_user_obj, Scope.USERS_MANAGE)
        if user_id and user_id != current_user_obj.user_id:
            # Updating another user - requires the users.manage scope
            if not may_manage_users:
                raise InsufficientPermissions(
                    "The users.manage scope is required to update other users"
                )
            target_user = await self.get_user(user_id)
            if not target_user:
                raise InvalidDataError("User not found")
        else:
            # Updating own profile
            target_user = current_user_obj

        # Update role (requires the users.manage scope)
        if role:
            if not may_manage_users:
                raise InsufficientPermissions(
                    "The users.manage scope is required to update user roles"
                )

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

        # Update player_filter and provider_filter (requires the users.manage scope)
        if player_filter is not None or provider_filter is not None:
            if not may_manage_users:
                raise InsufficientPermissions(
                    "The users.manage scope is required to update player/provider filters"
                )
            target_user = await self.update_user_filters(
                target_user, player_filter, provider_filter
            )

        # Update password if provided
        if password:
            await self._update_profile_password(
                target_user, password, may_manage_users, current_user_obj
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

        self.logger.info("User '%s' logged out", user.username)

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

    @api_command("auth/user/unlink_provider", required_scope=Scope.USERS_MANAGE)
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

        self.logger.info(
            "Auth provider '%s' unlinked from user (user_id=%s)",
            provider_type,
            user_id,
        )
        return True

    # ==================== Join Code Methods ====================

    async def generate_join_code(
        self,
        user: User,
        expires_in_hours: int = JOIN_CODE_DEFAULT_EXPIRY_HOURS,
        max_uses: int = 1,
        device_name: str = "Short Code Login",
    ) -> tuple[str, datetime]:
        """
        Generate a short join code for link/QR-based login.

        This creates a short alphanumeric code that can be exchanged for a JWT token.
        Used for features like the party provider guest access, device pairing,
        or other short-code authentication flows.

        :param user: The guest user that tokens created from this code will belong to.
        :param expires_in_hours: Hours until code expires (default: 8).
        :param max_uses: Maximum number of uses (0 = unlimited).
        :param device_name: Device name for tokens created with this code.
        :return: Tuple of (code, expires_at datetime).
        """
        if expires_in_hours <= 0:
            raise ValueError("expires_in_hours must be positive")
        if max_uses < 0:
            raise ValueError("max_uses must be non-negative (0 = unlimited)")
        if user.role != UserRole.GUEST:
            raise ValueError("Join codes can only be generated for guest accounts")

        now = utc()
        expires_at = now + timedelta(hours=expires_in_hours)

        for _ in range(3):  # Try up to 3 times to avoid code collisions
            code = "".join(secrets.choice(JOIN_CODE_CHARSET) for _ in range(JOIN_CODE_LENGTH))
            code_data = {
                "code_id": secrets.token_urlsafe(32),
                "code": code,
                "user_id": user.user_id,
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "max_uses": max_uses,
                "use_count": 0,
                "device_name": device_name,
            }
            try:
                await self.database.insert("join_codes", code_data)
                await self.database.commit()
                self.logger.info(
                    "Join code generated for user %s (expires: %s, max_uses: %s)",
                    user.username,
                    expires_at,
                    max_uses,
                )
                return code, expires_at
            except IntegrityError:
                self.logger.warning("Join code collision, retrying...")
                continue

        raise RuntimeError("Failed to generate a unique join code after 3 attempts")

    async def revoke_join_codes(self, user: User) -> int:
        """
        Revoke all join codes for a user.

        :param user: The user whose join codes should be revoked.
        :return: Number of codes revoked.
        """
        cursor = await self.database.execute(
            "DELETE FROM join_codes WHERE user_id = :user_id",
            {"user_id": user.user_id},
        )
        await self.database.commit()

        count = int(cursor.rowcount)
        if count > 0:
            self.logger.info("Revoked %d join code(s) for user %s", count, user.username)
        return count

    async def get_active_join_code(self, user: User) -> str | None:
        """
        Get the most recently created, non-expired join code for a user.

        :param user: The user to look up codes for.
        :return: The join code string if found, None otherwise.
        """
        now = utc()
        cursor = await self.database.execute(
            """
            SELECT code FROM join_codes
            WHERE user_id = :user_id
            AND expires_at > :now
            AND (max_uses = 0 OR use_count < max_uses)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"user_id": user.user_id, "now": now.isoformat()},
        )
        row = await cursor.fetchone()
        return str(row["code"]) if row else None

    async def get_join_code_expiry(self, code: str, user: User | None = None) -> datetime | None:
        """
        Get the expiry datetime for an active join code.

        :param code: The join code to look up.
        :param user: Optional user that must own the join code.
        :return: The expiry datetime if the code is active, None otherwise.
        """
        query = """
            SELECT expires_at FROM join_codes
            WHERE code = :code
            AND expires_at > :now
            AND (max_uses = 0 OR use_count < max_uses)
            """
        params: dict[str, Any] = {"code": code.upper(), "now": utc().isoformat()}
        if user is not None:
            query += "AND user_id = :user_id "
            params["user_id"] = user.user_id
        cursor = await self.database.execute(query + "LIMIT 1", params)
        row = await cursor.fetchone()
        return datetime.fromisoformat(str(row["expires_at"])) if row else None

    @api_command("auth/join_code/exchange", authenticated=False)
    async def exchange_join_code(self, code: str) -> dict[str, Any]:
        """
        Exchange a join code for an access token (public API).

        This is the public API endpoint for short-code authentication.
        Clients call this with a code (e.g., from QR scan or link) to receive a JWT token.

        :param code: The short join code.
        :return: Authentication result with access token if successful.
        """
        rate_limit_key, key_is_exclusive = _join_code_rate_limit_key()
        async with self._join_code_exchange_lock:
            if throttled := await self._check_join_code_rate_limit(rate_limit_key):
                return throttled

            token = await self._exchange_join_code(code)

            if not token:
                await self._join_code_rate_limiter.record_failed_attempt(rate_limit_key)
                await self._join_code_global_rate_limiter.record_failed_attempt(
                    JOIN_CODE_GLOBAL_RATE_LIMIT_KEY
                )
                return {
                    "success": False,
                    "error": "Invalid or expired join code",
                }

            # A bucket is only cleared when it belongs to one caller alone, so presenting a
            # valid code never lifts the throttle for anyone else.
            if key_is_exclusive:
                await self._join_code_rate_limiter.clear_attempts(rate_limit_key)

        # Decode token to get user info
        try:
            payload = self.jwt_helper.decode_token(token)
            return {
                "success": True,
                "access_token": token,
                "user": {
                    "user_id": payload.get("sub"),
                    "username": payload.get("username"),
                    "role": payload.get("role"),
                },
            }
        except pyjwt.InvalidTokenError:
            return {
                "success": False,
                "error": "Failed to create access token",
            }

    @api_command("auth/join_codes", required_scope=Scope.USERS_MANAGE)
    async def list_join_codes(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """
        List join codes, optionally filtered by user (admin only).

        :param user_id: Optional user ID to filter codes for.
        :return: List of join code records.
        """
        filter_args = {"user_id": user_id} if user_id else None
        rows = await self.database.get_rows("join_codes", filter_args, limit=100)
        return [dict(row) for row in rows]

    @api_command("auth/join_code/revoke", required_scope=Scope.USERS_MANAGE)
    async def revoke_join_code(self, code_id: str) -> None:
        """
        Revoke a specific join code (admin only).

        :param code_id: The code ID to revoke.
        """
        code_row = await self.database.get_row("join_codes", {"code_id": code_id})
        if not code_row:
            raise InvalidDataError("Join code not found")

        await self.database.delete("join_codes", {"code_id": code_id})
        await self.database.commit()
        self.logger.info("Join code revoked (code_id=%s)", code_id)

    async def _setup_database(self) -> None:
        """Set up database schema and handle migrations."""
        # Always create tables if they don't exist
        await self._create_database_tables()

        # Check current schema version
        try:
            if db_row := await self.database.get_row("settings", {"key": "schema_version"}):
                prev_version = int(db_row["value"])
            else:
                prev_version = DB_SCHEMA_VERSION
        except KeyError, ValueError, Exception:
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
        # Users table
        await self.database.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                display_name TEXT,
                avatar_url TEXT,
                preferences json NOT NULL DEFAULT '{}',
                player_filter json NOT NULL DEFAULT '[]',
                provider_filter json NOT NULL DEFAULT '[]'
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
                is_long_lived INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
        )
        # Join codes table (for short code to JWT exchange, used by providers like party)
        await self.database.execute(
            """
            CREATE TABLE IF NOT EXISTS join_codes (
                code_id TEXT PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                max_uses INTEGER DEFAULT 0,
                use_count INTEGER DEFAULT 0,
                last_used_at TEXT,
                device_name TEXT,
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
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS idx_join_codes_user ON join_codes(user_id)"
        )

    async def _migrate_database(self, from_version: int) -> None:
        """
        Perform database migration.

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

        # Migration to version 3: Add player_filter and provider_filter columns
        if from_version < 3:
            with contextlib.suppress(OperationalError):
                # Column(s) may already exist
                await self.database.execute(
                    "ALTER TABLE users ADD COLUMN player_filter json NOT NULL DEFAULT '[]'"
                )
                await self.database.execute(
                    "ALTER TABLE users ADD COLUMN provider_filter json NOT NULL DEFAULT '[]'"
                )
            await self.database.commit()

        # Migration to version 4: Make usernames case-insensitive by converting to lowercase
        if from_version < 4:
            await self.database.execute("UPDATE users SET username = LOWER(username)")
            await self.database.commit()

        # Migration to version 5: Add join codes table
        if from_version < 5:
            await self.database.execute(
                """
                CREATE TABLE IF NOT EXISTS join_codes (
                    code_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    max_uses INTEGER DEFAULT 0,
                    use_count INTEGER DEFAULT 0,
                    last_used_at TEXT,
                    device_name TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
                """
            )
            await self.database.commit()

    async def _get_or_create_jwt_secret(self) -> str:
        """
        Get or create JWT secret key from database.

        :return: JWT secret key for signing tokens.
        """
        # Try to get existing secret
        if secret_row := await self.database.get_row("settings", {"key": "jwt_secret"}):
            return str(secret_row["value"])

        # Generate new secret
        jwt_secret = JWTHelper.generate_secret_key()

        # Store in database
        await self.database.insert_or_replace(
            "settings",
            {"key": "jwt_secret", "value": jwt_secret, "type": "string"},
        )
        await self.database.commit()

        self.logger.info("Generated new JWT secret key")
        return jwt_secret

    async def _setup_login_providers(self) -> None:
        """Set up available login providers based on configuration."""
        # Always enable built-in provider
        self.login_providers["builtin"] = BuiltinLoginProvider(self.mass, "builtin", {})

        # Home Assistant OAuth provider
        # Automatically enabled if HA provider (plugin) is configured
        ha_provider = None
        for provider in self.mass.providers:
            if provider.domain == "hass" and provider.available:
                ha_provider = provider
                break

        if ha_provider:
            ha_provider = cast("HomeAssistantProvider", ha_provider)
            ha_url = ha_provider.url
            if not ha_url:
                self.logger.warning(
                    "Home Assistant provider has no URL configured, "
                    "Home Assistant OAuth login is not available"
                )
                return
            ha_config: HomeAssistantProviderConfig = {"ha_url": ha_url}
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
                ha_provider = cast("HomeAssistantProvider", ha_provider)
                ha_url = ha_provider.url
                if not ha_url:
                    # missing URL must never break the login providers endpoint,
                    # simply leave the HA OAuth provider unregistered
                    self.logger.debug(
                        "Home Assistant provider has no URL configured, "
                        "Home Assistant OAuth login is not available"
                    )
                    return
                ha_config: HomeAssistantProviderConfig = {"ha_url": ha_url}
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

    async def _has_non_system_users(self) -> bool:
        """Check if any non-system users exist."""
        user_rows = await self.database.get_rows("users", limit=10)
        return any(row["username"] != HOMEASSISTANT_SYSTEM_USER for row in user_rows)

    async def _migrate_system_user_role(self) -> None:
        """Migrate the Home Assistant system user of pre-existing installs to the service role."""
        user_row = await self.database.get_row(
            "users", {"username": normalize_username(HOMEASSISTANT_SYSTEM_USER)}
        )
        if user_row and user_row["role"] != UserRole.SERVICE.value:
            await self.database.update(
                "users", {"user_id": user_row["user_id"]}, {"role": UserRole.SERVICE.value}
            )
            self.logger.info(
                "Updated Home Assistant system user role to %s", UserRole.SERVICE.value
            )

    async def _prune_stale_user_filters(self) -> None:
        """Drop user access filter entries for providers or players that no longer exist."""
        known_providers = set(self.mass.config.get(CONF_PROVIDERS, {}))
        known_players = set(self.mass.config.get(CONF_PLAYERS, {}))
        # an empty config section means nothing is configured yet, which must not be
        # mistaken for everything having been removed
        await self._rewrite_user_filters(
            keep_provider=(lambda x: x in known_providers) if known_providers else None,
            keep_player=(lambda x: x in known_players) if known_players else None,
        )

    async def _rewrite_user_filters(
        self,
        keep_provider: Callable[[str], bool] | None,
        keep_player: Callable[[str], bool] | None,
        map_player: Callable[[str], str] | None = None,
    ) -> None:
        """
        Rewrite the access filters of all users.

        :param keep_provider: Returns False for the provider entries that must be dropped.
        :param keep_player: Returns False for the player entries that must be dropped.
        :param map_player: Maps a player entry onto its replacement, applied before keep_player.
        """
        if keep_provider is None and keep_player is None and map_player is None:
            return
        # removing a provider wipes the config of its players one by one, so without the lock
        # those rewrites would read the same filter and each undo the other's removal
        async with self._user_filter_lock:
            for row in await self.database.get_rows("users", limit=0):
                changed: dict[str, list[str]] = {}
                for column, keep_func, map_func in (
                    ("provider_filter", keep_provider, None),
                    ("player_filter", keep_player, map_player),
                ):
                    if keep_func is None and map_func is None:
                        continue
                    current: list[str] = json_loads(row[column])
                    remaining: list[str] = []
                    dropped: list[str] = []
                    for entry in current:
                        mapped = map_func(entry) if map_func else entry
                        if keep_func and not keep_func(mapped):
                            dropped.append(entry)
                        elif mapped not in remaining:
                            remaining.append(mapped)
                    if remaining == current:
                        continue
                    changed[column] = remaining
                    if not dropped:
                        self.logger.info(
                            "Updated the %s of user '%s' to %s",
                            column,
                            row["username"],
                            ", ".join(remaining),
                        )
                    elif remaining:
                        self.logger.info(
                            "Removed %s from the %s of user '%s'",
                            ", ".join(dropped),
                            column,
                            row["username"],
                        )
                    else:
                        # An empty filter means unrestricted. A user whose entries are all gone is
                        # deliberately left unrestricted, the alternative being an account that
                        # can see nothing at all.
                        self.logger.warning(
                            "Removed the last entries (%s) from the %s of user '%s'. This user is "
                            "no longer restricted, adjust the access settings if needed.",
                            ", ".join(dropped),
                            column,
                            row["username"],
                        )
                if changed:
                    await self.database.update(
                        "users",
                        {"user_id": row["user_id"]},
                        {column: json_dumps(value) for column, value in changed.items()},
                    )
                    # a session holds its own copy of the User object, so the live ones have to
                    # follow or they keep applying the filter that was just rewritten
                    self.webserver.update_active_user_filters(
                        row["user_id"],
                        player_filter=changed.get("player_filter"),
                        provider_filter=changed.get("provider_filter"),
                    )

    async def _migrate_playlog_to_first_user(self, user_id: str) -> None:
        """
        Migrate all existing playlog entries to the first user.

        This is called automatically when the first non-system user is created.
        All existing playlog entries (which have NULL userid) will be updated
        to belong to this first user.

        :param user_id: The user ID of the first user.
        """
        try:
            # Update all playlog entries with NULL userid to this user
            await self.mass.music.database.execute(
                f"UPDATE {DB_TABLE_PLAYLOG} SET userid = :userid WHERE userid IS NULL",
                {"userid": user_id},
            )
            await self.mass.music.database.commit()
            self.logger.info("Migrated existing playlog entries to first user: %s", user_id)
        except Exception as err:
            self.logger.warning("Failed to migrate playlog entries: %s", err)

    async def _update_profile_password(
        self,
        target_user: User,
        password: str,
        is_admin_update: bool,
        current_user: User,
    ) -> None:
        """Update user password (helper method)."""
        if len(password) < 8:
            raise InvalidDataError("Password must be at least 8 characters")

        builtin_provider = self.login_providers.get("builtin")
        if not builtin_provider or not isinstance(builtin_provider, BuiltinLoginProvider):
            raise InvalidDataError("Built-in auth not available")

        # Update password (used for both admin resets and user password changes)
        await builtin_provider.reset_password(target_user, password)

        if is_admin_update:
            self.logger.info(
                "Password reset for user %s by admin %s",
                target_user.username,
                current_user.username,
            )
        else:
            self.logger.info("Password changed for user %s", target_user.username)

    async def _check_join_code_rate_limit(self, key: str) -> dict[str, Any] | None:
        """
        Check the join code exchange throttles that apply to the calling client.

        :param key: Rate limit key identifying the calling client.
        :return: The error result to return to the caller, or None if the attempt may proceed.
        """
        limiters = (
            ("client", self._join_code_rate_limiter, key),
            ("server", self._join_code_global_rate_limiter, JOIN_CODE_GLOBAL_RATE_LIMIT_KEY),
        )
        for scope, limiter, limiter_key in limiters:
            allowed, remaining_delay = await limiter.check_rate_limit(limiter_key)
            if allowed:
                continue
            # The attempted code is deliberately absent here: it has not been checked yet,
            # so it may well be a valid one. Each failure that filled the bucket already
            # logged its own (rejected, and therefore unusable) code.
            self.logger.warning(
                "Join code exchange throttled by the %s limit "
                "(client=%s, client_failures=%d, server_failures=%d). "
                "%d seconds remaining.",
                scope,
                key,
                self._join_code_rate_limiter.get_attempt_count(key),
                self._join_code_global_rate_limiter.get_attempt_count(
                    JOIN_CODE_GLOBAL_RATE_LIMIT_KEY
                ),
                remaining_delay,
            )
            return {
                "success": False,
                "error": (
                    f"Too many failed attempts. Please try again in {remaining_delay} seconds."
                ),
            }
        return None

    async def _exchange_join_code(self, code: str) -> str | None:
        """
        Exchange a join code for a JWT access token.

        The token is created for the user associated with the join code.

        :param code: The short join code.
        :return: JWT token string if valid, None otherwise.
        """
        now = utc()

        cursor = await self.database.execute(
            """
            UPDATE join_codes
            SET use_count = use_count + 1,
                last_used_at = :now
            WHERE code = :code
            AND expires_at > :now
            AND (max_uses = 0 OR use_count < max_uses)
            RETURNING user_id, device_name
            """,
            {"now": now.isoformat(), "code": code.upper()},
        )
        row = await cursor.fetchone()
        await self.database.commit()

        if not row:
            self.logger.warning(
                "Join code exchange rejected (client=%s, code=%s)",
                get_current_client_id() or JOIN_CODE_ANONYMOUS_RATE_LIMIT_KEY,
                _mask_join_code(code),
            )
            return None

        user = await self.get_user(row["user_id"])
        if not user:
            self.logger.error(
                "User not found for join code despite FK constraint (user_id=%s)", row["user_id"]
            )
            return None

        device_name = row["device_name"] or "Short Code Login"
        token = await self.create_token(
            user,
            device_name,
            is_long_lived=False,
        )

        self.logger.info(
            "Join code exchanged for token (user=%s)",
            user.username,
        )
        return token

    async def _cleanup_expired_join_codes(self) -> None:
        """Delete expired and exhausted join codes from the database."""
        now = utc()
        cursor = await self.database.execute(
            """
            DELETE FROM join_codes
            WHERE expires_at < :now
               OR (max_uses > 0 AND use_count >= max_uses)
            """,
            {"now": now.isoformat()},
        )
        await self.database.commit()
        count = int(cursor.rowcount)
        if count > 0:
            self.logger.debug("Cleaned up %d expired/exhausted join code(s)", count)

    def _schedule_join_code_cleanup(self) -> None:
        """Schedule periodic cleanup of expired join codes."""
        self.mass.create_task(self._cleanup_expired_join_codes())
        self.mass.call_later(86400, self._schedule_join_code_cleanup)

    async def _refresh_token_expiration(
        self, token_row: Mapping[str, Any], user: User, is_long_lived: bool
    ) -> dict[str, str] | None:
        """
        Build the on-use column updates for a token, enforcing the absolute lifetime cap.

        :param token_row: The auth_tokens row for the token being used.
        :param user: The user owning the token.
        :param is_long_lived: Whether the token is long-lived.
        :return: Column updates to apply (empty when the stored activity timestamp is
            still fresh, so callers can skip the write), or None if the token exceeded
            its max lifetime (in which case the token row is deleted).
        """
        now = utc()

        if not is_long_lived:
            created_at = datetime.fromisoformat(token_row["created_at"])
            if now > created_at + timedelta(days=TOKEN_ABSOLUTE_MAX_EXPIRATION):
                await self.database.delete("auth_tokens", {"token_id": token_row["token_id"]})
                return None

        # The HTTP API authenticates on every request, so persisting activity per use
        # would cost an UPDATE+commit (an fsync) per request. Skip the write while the
        # stored timestamp is fresh; last_used_at and the sliding expiration then lag
        # by at most this interval, which is negligible against the 30-day idle window.
        if last_used_at := token_row["last_used_at"]:
            if now - datetime.fromisoformat(last_used_at) < TOKEN_ACTIVITY_PERSIST_INTERVAL:
                return {}

        updates = {"last_used_at": now.isoformat()}
        if not is_long_lived and user.role != UserRole.GUEST:
            # Short-lived token: extend expiration on each use (sliding window)
            new_expires_at = now + timedelta(days=TOKEN_SHORT_LIVED_EXPIRATION)
            updates["expires_at"] = new_expires_at.isoformat()

        return updates

    async def _can_reuse_ha_integration_token(self, token: str, system_user: User) -> bool:
        """Check whether the stored HA integration token is valid and not yet due for rotation."""
        token_id = self.jwt_helper.get_token_id(token)
        if not token_id:
            return False
        token_row = await self.database.get_row("auth_tokens", {"token_id": token_id})
        if not token_row or token_row["user_id"] != system_user.user_id:
            return False
        now = utc()
        if token_row["expires_at"] and datetime.fromisoformat(token_row["expires_at"]) <= now:
            return False
        created_at = datetime.fromisoformat(token_row["created_at"])
        rotate_after = created_at + timedelta(
            days=TOKEN_ABSOLUTE_MAX_EXPIRATION - HA_TOKEN_ROTATION_MARGIN
        )
        return now < rotate_after

    def _notify_user_access_revoked(self, user: User) -> None:
        """Dispatch an access withdrawal to subscribers, isolating them from each other."""
        for callback in list(self._access_revoked_callbacks):
            self.mass.loop.call_soon(callback, user)


def _join_code_rate_limit_key() -> tuple[str, bool]:
    """
    Work out which bucket the calling client's failed join code exchanges belong to.

    :return: The rate limit key, and whether that key identifies a single caller
        exclusively (a shared key must never be cleared on a successful exchange).
    """
    if client_id := get_current_client_id():
        return client_id, True
    if peer_address := get_current_peer_address():
        return f"peer:{peer_address}", False
    return JOIN_CODE_ANONYMOUS_RATE_LIMIT_KEY, False


def _mask_join_code(code: str) -> str:
    """
    Mask a join code so support logs can correlate attempts without exposing a usable code.

    :param code: The join code as supplied by the client.
    :return: The code with everything past its prefix replaced by asterisks.
    """
    normalized = code.upper()
    return normalized[:4] + "*" * max(len(normalized) - 4, 0)
