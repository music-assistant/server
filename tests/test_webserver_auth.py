"""Tests for webserver authentication and user management."""

import asyncio
import hashlib
import logging
import pathlib
import threading
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from sqlite3 import IntegrityError
from typing import Any
from unittest.mock import patch

import pytest
from music_assistant_models.auth import AuthProviderType, Scope, User, UserRole
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError

from music_assistant.constants import (
    GUEST_ACCESS_RESTRICTED_PLAYER_ID,
    HOMEASSISTANT_SYSTEM_USER,
)
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.webserver.auth import (
    JOIN_CODE_LENGTH,
    TOKEN_ABSOLUTE_MAX_EXPIRATION,
    TOKEN_ACTIVITY_PERSIST_INTERVAL,
    TOKEN_GUEST_EXPIRATION,
    TOKEN_LONG_LIVED_EXPIRATION,
    TOKEN_SHORT_LIVED_EXPIRATION,
    AuthenticationManager,
)
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    ImpersonatedUser,
    get_current_user,
    has_scope,
    resolve_command_impersonation,
    set_current_token,
    set_current_user,
    set_impersonated_user,
)
from music_assistant.controllers.webserver.helpers.auth_providers import BuiltinLoginProvider
from music_assistant.helpers.datetime import utc
from music_assistant.mass import MusicAssistant


@pytest.fixture
async def mass_minimal(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant]:
    """
    Create a minimal Music Assistant instance for auth testing without starting the webserver.

    :param tmp_path: Temporary directory for test data.
    """
    storage_path = tmp_path / "data"
    cache_path = tmp_path / "cache"
    storage_path.mkdir(parents=True)
    cache_path.mkdir(parents=True)

    # Suppress aiosqlite debug logging
    logging.getLogger("aiosqlite").level = logging.INFO

    mass_instance = MusicAssistant(str(storage_path), str(cache_path))

    # Initialize the minimum required for auth testing
    mass_instance.loop = asyncio.get_running_loop()
    # fixture runs on the event loop thread, like MusicAssistant.start()
    mass_instance.loop_thread_id = threading.get_ident()

    # Create config controller
    mass_instance.config = ConfigController(mass_instance)
    await mass_instance.config.setup()

    # Create webserver controller (but don't start the actual server)
    webserver = WebserverController(mass_instance)
    mass_instance.webserver = webserver

    # Get webserver config and manually set it (avoids starting the server)
    webserver_config = await mass_instance.config.get_core_config("webserver")
    webserver.config = webserver_config

    # Setup auth manager only (not the full webserver with routes/sockets)
    await webserver.auth.setup()

    try:
        yield mass_instance
    finally:
        # Cleanup
        await webserver.auth.close()
        await mass_instance.config.close()


@pytest.fixture
async def auth_manager(mass_minimal: MusicAssistant) -> AuthenticationManager:
    """
    Get authentication manager from mass instance.

    :param mass_minimal: Minimal MusicAssistant instance.
    """
    return mass_minimal.webserver.auth


async def test_auth_manager_initialization(auth_manager: AuthenticationManager) -> None:
    """
    Test that the authentication manager initializes correctly.

    :param auth_manager: AuthenticationManager instance.
    """
    assert auth_manager is not None
    assert auth_manager.database is not None
    assert "builtin" in auth_manager.login_providers
    assert isinstance(auth_manager.login_providers["builtin"], BuiltinLoginProvider)


async def test_has_users_initially_empty(auth_manager: AuthenticationManager) -> None:
    """
    Test that has_users returns False when no users exist.

    :param auth_manager: AuthenticationManager instance.
    """
    has_users = auth_manager.has_users
    assert has_users is False


async def test_create_user(auth_manager: AuthenticationManager) -> None:
    """
    Test creating a new user.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(
        username="testuser",
        role=UserRole.USER,
        display_name="Test User",
    )

    assert user is not None
    assert user.username == "testuser"
    assert user.role == UserRole.USER
    assert user.display_name == "Test User"
    assert user.enabled is True
    assert user.user_id is not None

    # Verify user exists in database
    has_users = auth_manager.has_users
    assert has_users is True


async def test_get_user(auth_manager: AuthenticationManager) -> None:
    """
    Test retrieving a user by ID.

    :param auth_manager: AuthenticationManager instance.
    """
    # Create a user first
    created_user = await auth_manager.create_user(username="getuser", role=UserRole.USER)

    # Set current user for authorization (get_user requires admin role)
    admin_user = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    set_current_user(admin_user)

    # Retrieve the user
    retrieved_user = await auth_manager.get_user(created_user.user_id)

    assert retrieved_user is not None
    assert retrieved_user.user_id == created_user.user_id
    assert retrieved_user.username == created_user.username


async def test_create_user_with_builtin_provider(auth_manager: AuthenticationManager) -> None:
    """
    Test creating a user with built-in authentication.

    :param auth_manager: AuthenticationManager instance.
    """
    builtin_provider = auth_manager.login_providers.get("builtin")
    assert builtin_provider is not None
    assert isinstance(builtin_provider, BuiltinLoginProvider)

    user = await builtin_provider.create_user_with_password(
        username="testuser2",
        password="testpassword123",
        role=UserRole.USER,
    )

    assert user is not None
    assert user.username == "testuser2"


async def test_authenticate_with_password(auth_manager: AuthenticationManager) -> None:
    """
    Test authenticating with username and password.

    :param auth_manager: AuthenticationManager instance.
    """
    builtin_provider = auth_manager.login_providers.get("builtin")
    assert builtin_provider is not None
    assert isinstance(builtin_provider, BuiltinLoginProvider)

    # Create user with password
    await builtin_provider.create_user_with_password(
        username="authtest",
        password="secure_password_123",
        role=UserRole.USER,
    )

    # Test successful authentication
    result = await auth_manager.authenticate_with_credentials(
        "builtin",
        {"username": "authtest", "password": "secure_password_123"},
    )

    assert result.success is True
    assert result.user is not None
    assert result.user.username == "authtest"
    # Note: Built-in provider doesn't auto-generate access token on login,
    # that's done by the web login flow. We just verify authentication succeeds.

    # Test failed authentication with wrong password
    result = await auth_manager.authenticate_with_credentials(
        "builtin",
        {"username": "authtest", "password": "wrong_password"},
    )

    assert result.success is False
    assert result.user is None
    assert result.error is not None


async def test_create_token(auth_manager: AuthenticationManager) -> None:
    """
    Test creating access tokens.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="tokenuser", role=UserRole.USER)

    # Create short-lived token
    short_token = await auth_manager.create_token(user, "Test Device", is_long_lived=False)
    assert short_token is not None
    assert len(short_token) > 0

    # Create long-lived token
    long_token = await auth_manager.create_token(user, "API Key", is_long_lived=True)
    assert long_token is not None
    assert len(long_token) > 0
    assert long_token != short_token


async def test_authenticate_with_token(auth_manager: AuthenticationManager) -> None:
    """
    Test authenticating with an access token.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="tokenauth", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Test Token", is_long_lived=False)

    # Authenticate with token
    authenticated_user = await auth_manager.authenticate_with_token(token)

    assert authenticated_user is not None
    assert authenticated_user.user_id == user.user_id
    assert authenticated_user.username == user.username


async def test_token_expiration(auth_manager: AuthenticationManager) -> None:
    """
    Test that expired tokens are rejected.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="expireuser", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Expire Test", is_long_lived=False)

    # Hash the token to look it up
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None

    # Manually expire the token by setting expires_at in the past
    past_time = utc() - timedelta(days=1)
    await auth_manager.database.update(
        "auth_tokens",
        {"token_id": token_row["token_id"]},
        {"expires_at": past_time.isoformat()},
    )

    # Try to authenticate with expired token
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is None


async def test_update_user_profile(auth_manager: AuthenticationManager) -> None:
    """
    Test updating user profile information.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(
        username="updateuser",
        role=UserRole.USER,
        display_name="Original Name",
    )

    # Update user profile
    updated_user = await auth_manager.update_user(
        user,
        display_name="New Name",
        avatar_url="https://example.com/avatar.jpg",
    )

    assert updated_user is not None
    assert updated_user.display_name == "New Name"
    assert updated_user.avatar_url == "https://example.com/avatar.jpg"
    assert updated_user.username == user.username


async def test_change_password(auth_manager: AuthenticationManager) -> None:
    """
    Test changing user password.

    :param auth_manager: AuthenticationManager instance.
    """
    builtin_provider = auth_manager.login_providers.get("builtin")
    assert builtin_provider is not None
    assert isinstance(builtin_provider, BuiltinLoginProvider)

    # Create user with password
    user = await builtin_provider.create_user_with_password(
        username="pwdchange",
        password="old_password_123",
        role=UserRole.USER,
    )

    # Change password
    success = await builtin_provider.change_password(
        user,
        "old_password_123",
        "new_password_456",
    )
    assert success is True

    # Verify old password no longer works
    result = await auth_manager.authenticate_with_credentials(
        "builtin",
        {"username": "pwdchange", "password": "old_password_123"},
    )
    assert result.success is False

    # Verify new password works
    result = await auth_manager.authenticate_with_credentials(
        "builtin",
        {"username": "pwdchange", "password": "new_password_456"},
    )
    assert result.success is True


async def test_revoke_token(auth_manager: AuthenticationManager) -> None:
    """
    Test revoking an access token.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="revokeuser", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Revoke Test", is_long_lived=False)

    # Set current user context for authorization
    set_current_user(user)

    # Get token_id
    token_id = await auth_manager.get_token_id_from_token(token)
    assert token_id is not None

    # Token should work before revocation
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is not None

    # Revoke the token
    await auth_manager.revoke_token(token_id)

    # Token should not work after revocation
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is None


async def test_list_users(auth_manager: AuthenticationManager) -> None:
    """
    Test listing all users (admin only).

    :param auth_manager: AuthenticationManager instance.
    """
    # Create admin user and set as current
    admin = await auth_manager.create_user(username="listadmin", role=UserRole.ADMIN)
    set_current_user(admin)

    # Create some test users
    await auth_manager.create_user(username="user1", role=UserRole.USER)
    await auth_manager.create_user(username="user2", role=UserRole.USER)

    # List all users
    users = await auth_manager.list_users()

    # Should not include system users
    usernames = [u.username for u in users]
    assert "listadmin" in usernames
    assert "user1" in usernames
    assert "user2" in usernames


async def test_disable_enable_user(auth_manager: AuthenticationManager) -> None:
    """
    Test disabling and enabling user accounts.

    :param auth_manager: AuthenticationManager instance.
    """
    # Create admin and regular user
    admin = await auth_manager.create_user(username="disableadmin", role=UserRole.ADMIN)
    user = await auth_manager.create_user(username="disableuser", role=UserRole.USER)

    # Set admin as current user
    set_current_user(admin)

    # Disable the user
    await auth_manager.disable_user(user.user_id)

    # Verify user is disabled
    disabled_user = await auth_manager.get_user(user.user_id)
    assert disabled_user is None  # get_user filters out disabled users

    # Enable the user
    await auth_manager.enable_user(user.user_id)

    # Verify user is enabled
    enabled_user = await auth_manager.get_user(user.user_id)
    assert enabled_user is not None


async def test_cannot_disable_own_account(auth_manager: AuthenticationManager) -> None:
    """
    Test that users cannot disable their own account.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="selfadmin", role=UserRole.ADMIN)
    set_current_user(admin)

    # Try to disable own account
    with pytest.raises(InvalidDataError, match="Cannot disable your own account"):
        await auth_manager.disable_user(admin.user_id)


async def test_user_preferences(auth_manager: AuthenticationManager) -> None:
    """
    Test updating user preferences.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="prefuser", role=UserRole.USER)

    # Update preferences
    preferences = {"theme": "dark", "language": "en"}
    updated_user = await auth_manager.update_user_preferences(user, preferences)

    assert updated_user is not None
    assert updated_user.preferences == preferences


async def test_link_user_to_provider(auth_manager: AuthenticationManager) -> None:
    """
    Test linking user to authentication provider.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="linkuser", role=UserRole.USER)

    # Link to provider
    link = await auth_manager.link_user_to_provider(
        user,
        AuthProviderType.HOME_ASSISTANT,
        "ha_user_123",
    )

    assert link is not None
    assert link.user_id == user.user_id
    assert link.provider_type == AuthProviderType.HOME_ASSISTANT
    assert link.provider_user_id == "ha_user_123"

    # Retrieve user by provider link
    retrieved_user = await auth_manager.get_user_by_provider_link(
        AuthProviderType.HOME_ASSISTANT,
        "ha_user_123",
    )

    assert retrieved_user is not None
    assert retrieved_user.user_id == user.user_id


async def test_homeassistant_system_user(auth_manager: AuthenticationManager) -> None:
    """
    Test Home Assistant system user creation.

    :param auth_manager: AuthenticationManager instance.
    """
    # Get or create system user
    system_user = await auth_manager.get_homeassistant_system_user()

    assert system_user is not None
    assert system_user.username == HOMEASSISTANT_SYSTEM_USER
    assert system_user.display_name == "Home Assistant Integration"
    assert system_user.role == UserRole.SERVICE

    # Getting it again should return the same user
    system_user2 = await auth_manager.get_homeassistant_system_user()
    assert system_user2.user_id == system_user.user_id


async def test_homeassistant_system_user_token_stable_across_restarts(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that a valid Home Assistant integration token is reused on repeated announces.

    The addon announces on every startup; re-minting each time would invalidate the
    token the HA integration still holds (issue #158174). Repeated calls must return
    the exact same token so the re-announce is idempotent.

    :param auth_manager: AuthenticationManager instance.
    """
    token1 = await auth_manager.get_homeassistant_system_user_token()
    assert token1 is not None

    # A later startup (restart) returns the same token unchanged.
    token2 = await auth_manager.get_homeassistant_system_user_token()
    assert token2 == token1

    user = await auth_manager.authenticate_with_token(token1)
    assert user is not None
    assert user.username == HOMEASSISTANT_SYSTEM_USER


async def test_homeassistant_system_user_token_reissued_when_invalid(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that a fresh token is minted once the existing one is revoked or gone.

    :param auth_manager: AuthenticationManager instance.
    """
    token1 = await auth_manager.get_homeassistant_system_user_token()

    # Drop the token row, as would happen if it expired or was revoked.
    token_id = auth_manager.jwt_helper.get_token_id(token1)
    await auth_manager.database.delete("auth_tokens", {"token_id": token_id})

    token2 = await auth_manager.get_homeassistant_system_user_token()
    assert token2 != token1
    assert await auth_manager.authenticate_with_token(token2) is not None
    assert await auth_manager.authenticate_with_token(token1) is None


async def test_homeassistant_system_user_token_rotated_before_absolute_max(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that the Home Assistant integration token is rotated before its absolute cap.

    The integration cannot reauth while running as an addon, so the token must be
    replaced (and re-announced) before the absolute lifetime cap silently strands
    the integration (issue #171938). The superseded token must remain valid so the
    integration keeps working until it reloads with the new one.

    :param auth_manager: AuthenticationManager instance.
    """
    token1 = await auth_manager.get_homeassistant_system_user_token()
    token_id = auth_manager.jwt_helper.get_token_id(token1)

    # Age the token into the rotation window (close to the absolute cap, still valid).
    now = utc()
    created_at = now - timedelta(days=TOKEN_ABSOLUTE_MAX_EXPIRATION - 1)
    await auth_manager.database.update(
        "auth_tokens",
        {"token_id": token_id},
        {
            "created_at": created_at.isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
        },
    )

    # The next (periodic) announce must mint a replacement.
    token2 = await auth_manager.get_homeassistant_system_user_token()
    assert token2 != token1

    # Both tokens work: the old one until it expires, the new one going forward.
    assert await auth_manager.authenticate_with_token(token1) is not None
    assert await auth_manager.authenticate_with_token(token2) is not None

    # The new token is stable again on subsequent announces.
    assert await auth_manager.get_homeassistant_system_user_token() == token2


async def test_homeassistant_system_user_token_cleans_up_expired_rows(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that expired Home Assistant integration token rows are removed on rotation.

    :param auth_manager: AuthenticationManager instance.
    """
    token1 = await auth_manager.get_homeassistant_system_user_token()
    token_id = auth_manager.jwt_helper.get_token_id(token1)

    # Expire the token entirely so the next announce mints a replacement.
    await auth_manager.database.update(
        "auth_tokens",
        {"token_id": token_id},
        {"expires_at": (utc() - timedelta(days=1)).isoformat()},
    )

    token2 = await auth_manager.get_homeassistant_system_user_token()
    assert token2 != token1
    assert await auth_manager.database.get_row("auth_tokens", {"token_id": token_id}) is None


async def test_update_user_role(auth_manager: AuthenticationManager) -> None:
    """
    Test updating user role (admin only).

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="roleadmin", role=UserRole.ADMIN)
    user = await auth_manager.create_user(username="roleuser", role=UserRole.USER)

    # Update role
    success = await auth_manager.update_user_role(user.user_id, UserRole.ADMIN, admin)
    assert success is True

    # Verify role was updated
    set_current_user(admin)
    updated_user = await auth_manager.get_user(user.user_id)
    assert updated_user is not None
    assert updated_user.role == UserRole.ADMIN


async def test_delete_user(auth_manager: AuthenticationManager) -> None:
    """
    Test deleting a user account.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="deleteadmin", role=UserRole.ADMIN)
    user = await auth_manager.create_user(username="deleteuser", role=UserRole.USER)

    # Set admin as current user
    set_current_user(admin)

    # Delete the user
    await auth_manager.delete_user(user.user_id)

    # Verify user is deleted
    deleted_user = await auth_manager.get_user(user.user_id)
    assert deleted_user is None


async def test_cannot_delete_own_account(auth_manager: AuthenticationManager) -> None:
    """
    Test that users cannot delete their own account.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="selfdeleteadmin", role=UserRole.ADMIN)
    set_current_user(admin)

    # Try to delete own account
    with pytest.raises(InvalidDataError, match="Cannot delete your own account"):
        await auth_manager.delete_user(admin.user_id)


async def test_get_user_tokens(auth_manager: AuthenticationManager) -> None:
    """
    Test getting user's tokens.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="tokensuser", role=UserRole.USER)
    set_current_user(user)

    # Create some tokens
    await auth_manager.create_token(user, "Device 1", is_long_lived=False)
    await auth_manager.create_token(user, "Device 2", is_long_lived=True)

    # Get user tokens
    tokens = await auth_manager.get_user_tokens(user.user_id)

    assert len(tokens) == 2
    token_names = [t.name for t in tokens]
    assert "Device 1" in token_names
    assert "Device 2" in token_names


async def test_get_login_providers(auth_manager: AuthenticationManager) -> None:
    """
    Test getting available login providers.

    :param auth_manager: AuthenticationManager instance.
    """
    providers = await auth_manager.get_login_providers()

    assert len(providers) > 0
    assert any(p["provider_id"] == "builtin" for p in providers)


async def test_create_user_with_api(auth_manager: AuthenticationManager) -> None:
    """
    Test creating user via API command.

    :param auth_manager: AuthenticationManager instance.
    """
    # Create admin user and set as current
    admin = await auth_manager.create_user(username="apiadmin", role=UserRole.ADMIN)
    set_current_user(admin)

    # Create user via API
    user = await auth_manager.create_user_with_api(
        username="apiuser",
        password="password123",
        role="user",
        display_name="API User",
    )

    assert user is not None
    assert user.username == "apiuser"
    assert user.role == UserRole.USER
    assert user.display_name == "API User"


async def test_create_user_api_validation(auth_manager: AuthenticationManager) -> None:
    """
    Test validation in create_user_with_api.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="validadmin", role=UserRole.ADMIN)
    set_current_user(admin)

    # Test username too short
    with pytest.raises(InvalidDataError, match="Username must be at least 2 characters"):
        await auth_manager.create_user_with_api(
            username="a",
            password="password123",
        )

    # Test 2-character username is accepted (minimum allowed)
    user_2char = await auth_manager.create_user_with_api(
        username="ab",
        password="password123",
    )
    assert user_2char.username == "ab"

    # Test password too short
    with pytest.raises(InvalidDataError, match="Password must be at least 8 characters"):
        await auth_manager.create_user_with_api(
            username="validuser",
            password="short",
        )


async def test_logout(auth_manager: AuthenticationManager) -> None:
    """
    Test logout functionality.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="logoutuser", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Logout Test", is_long_lived=False)

    # Set current user and token
    set_current_user(user)
    set_current_token(token)

    # Token should work before logout
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is not None

    # Logout
    await auth_manager.logout()

    # Token should not work after logout
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is None


async def test_token_sliding_expiration(auth_manager: AuthenticationManager) -> None:
    """
    Test that short-lived tokens auto-renew on use.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="slideuser", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Slide Test", is_long_lived=False)

    # Get initial expiration
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None
    initial_expires_at = token_row["expires_at"]

    # Use the token (authenticate)
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is not None

    # Check that expiration was updated
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None
    updated_expires_at = token_row["expires_at"]

    # Expiration should have been extended
    assert updated_expires_at != initial_expires_at


async def test_long_lived_token_no_auto_renewal(auth_manager: AuthenticationManager) -> None:
    """
    Test that long-lived tokens do NOT auto-renew on use.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="longuser", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Long Test", is_long_lived=True)

    # Get initial expiration
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None
    initial_expires_at = token_row["expires_at"]

    # Use the token (authenticate)
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is not None

    # Check that expiration was NOT updated
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None
    updated_expires_at = token_row["expires_at"]

    # Expiration should remain the same for long-lived tokens
    assert updated_expires_at == initial_expires_at


async def test_token_activity_write_throttled(
    auth_manager: AuthenticationManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Test that rapid authentications persist the token activity only once.

    The HTTP API authenticates on every request; the activity timestamp must not be
    written to the database again while the stored one is still fresh.

    :param auth_manager: AuthenticationManager instance.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    user = await auth_manager.create_user(username="throttleuser", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Throttle Test", is_long_lived=False)

    token_update_count = 0
    original_update = auth_manager.database.update

    async def counting_update(table: str, match: dict[str, Any], values: dict[str, Any]) -> None:
        nonlocal token_update_count
        if table == "auth_tokens":
            token_update_count += 1
        await original_update(table, match, values)

    monkeypatch.setattr(auth_manager.database, "update", counting_update)

    # Two rapid authentications: only the first persists the activity timestamp.
    assert await auth_manager.authenticate_with_token(token) is not None
    assert await auth_manager.authenticate_with_token(token) is not None
    assert token_update_count == 1


async def test_token_activity_write_resumes_after_interval(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that token activity is persisted again once the stored timestamp is stale.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="throttleresume", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Throttle Resume Test", is_long_lived=False)
    assert await auth_manager.authenticate_with_token(token) is not None

    # Age the stored activity timestamp (and sliding expiration) past the persist interval.
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None
    stale_time = utc() - TOKEN_ACTIVITY_PERSIST_INTERVAL - timedelta(minutes=5)
    stale_expires = stale_time + timedelta(days=TOKEN_SHORT_LIVED_EXPIRATION)
    await auth_manager.database.update(
        "auth_tokens",
        {"token_id": token_row["token_id"]},
        {"last_used_at": stale_time.isoformat(), "expires_at": stale_expires.isoformat()},
    )

    assert await auth_manager.authenticate_with_token(token) is not None

    # Both the activity timestamp and the sliding expiration must be persisted again.
    updated_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert updated_row is not None
    assert datetime.fromisoformat(updated_row["last_used_at"]) > stale_time
    assert datetime.fromisoformat(updated_row["expires_at"]) > stale_expires


async def test_revoked_token_rejected_within_throttle_window(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that revocation takes effect immediately while the activity write is throttled.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="throttlerevoke", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Throttle Revoke Test", is_long_lived=False)
    set_current_user(user)

    # First use persists a fresh activity timestamp (entering the throttle window).
    assert await auth_manager.authenticate_with_token(token) is not None

    token_id = await auth_manager.get_token_id_from_token(token)
    assert token_id is not None
    await auth_manager.revoke_token(token_id)

    # The throttle only affects the activity write, never the validation reads.
    assert await auth_manager.authenticate_with_token(token) is None


async def test_expired_token_rejected_despite_fresh_activity(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that expiry validation is not affected by a fresh activity timestamp.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="throttleexpired", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Throttle Expired Test", is_long_lived=False)

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None
    await auth_manager.database.update(
        "auth_tokens",
        {"token_id": token_row["token_id"]},
        {
            "expires_at": (utc() - timedelta(days=1)).isoformat(),
            "last_used_at": utc().isoformat(),
        },
    )

    assert await auth_manager.authenticate_with_token(token) is None


async def test_token_absolute_max_enforced_despite_fresh_activity(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that the absolute lifetime cap is enforced even with a fresh activity timestamp.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="throttleabsmax", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Throttle Abs Max Test", is_long_lived=False)

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None
    created_at = utc() - timedelta(days=TOKEN_ABSOLUTE_MAX_EXPIRATION + 1)
    future_expires = utc() + timedelta(days=TOKEN_SHORT_LIVED_EXPIRATION)
    await auth_manager.database.update(
        "auth_tokens",
        {"token_id": token_row["token_id"]},
        {
            "created_at": created_at.isoformat(),
            "expires_at": future_expires.isoformat(),
            "last_used_at": utc().isoformat(),
        },
    )

    assert await auth_manager.authenticate_with_token(token) is None
    assert await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash}) is None


async def test_long_lived_token_default_is_one_year() -> None:
    """Test that the long-lived token default lifetime is 365 days."""
    assert TOKEN_LONG_LIVED_EXPIRATION == 365


async def test_token_absolute_max_lifetime(auth_manager: AuthenticationManager) -> None:
    """
    Test that a short-lived token past its absolute max lifetime cannot be renewed.

    The sliding window keeps a session alive on use, but a token created longer than
    the absolute maximum ago must be rejected regardless of the sliding expiration.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="absmaxuser", role=UserRole.USER)
    token = await auth_manager.create_token(user, "Abs Max Test", is_long_lived=False)

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None

    # Created past the absolute max but with a future sliding expires_at, so only the cap can reject it.
    created_at = utc() - timedelta(days=TOKEN_ABSOLUTE_MAX_EXPIRATION + 1)
    future_expires = utc() + timedelta(days=TOKEN_SHORT_LIVED_EXPIRATION)
    await auth_manager.database.update(
        "auth_tokens",
        {"token_id": token_row["token_id"]},
        {"created_at": created_at.isoformat(), "expires_at": future_expires.isoformat()},
    )

    # Token must be rejected and the row deleted.
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is None

    deleted_row = await auth_manager.database.get_row(
        "auth_tokens", {"token_id": token_row["token_id"]}
    )
    assert deleted_row is None


async def test_legacy_token_absolute_max_lifetime(auth_manager: AuthenticationManager) -> None:
    """
    Test that the absolute max lifetime is also enforced on the legacy hash-token path.

    Legacy (non-JWT) tokens authenticate via a hash lookup that shares the same cap logic,
    so a hash token created past the absolute maximum must be rejected and its row deleted.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="legacyabsmax", role=UserRole.USER)

    # A non-JWT token string forces the legacy hash-based lookup path.
    raw_token = "legacy-hash-token-absmax"
    token_id = "legacy-absmax-token-id"
    # Created past the absolute max but with a future sliding expires_at, so only the cap can reject it.
    created_at = utc() - timedelta(days=TOKEN_ABSOLUTE_MAX_EXPIRATION + 1)
    future_expires = utc() + timedelta(days=TOKEN_SHORT_LIVED_EXPIRATION)
    await auth_manager.database.insert(
        "auth_tokens",
        {
            "token_id": token_id,
            "user_id": user.user_id,
            "token_hash": hashlib.sha256(raw_token.encode()).hexdigest(),
            "name": "Legacy Abs Max Test",
            "created_at": created_at.isoformat(),
            "expires_at": future_expires.isoformat(),
            "is_long_lived": 0,
        },
    )

    # Token must be rejected and the row deleted.
    assert await auth_manager.authenticate_with_token(raw_token) is None
    assert await auth_manager.database.get_row("auth_tokens", {"token_id": token_id}) is None


async def test_revoke_tokens_for_user_persists(auth_manager: AuthenticationManager) -> None:
    """
    Test that revoke_tokens_for_user commits so the tokens no longer authenticate.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="guestrevoke", role=UserRole.GUEST)
    token = await auth_manager.create_token(user, "Guest Token", is_long_lived=False)

    # Token works before revocation
    assert await auth_manager.authenticate_with_token(token) is not None

    revoked = await auth_manager.revoke_tokens_for_user(user)
    assert revoked == 1

    # Reopen the raw connection to roll back any uncommitted tx: an uncommitted DELETE would resurrect the row.
    await auth_manager.database._db.close()
    await auth_manager.database.setup()

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    assert await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash}) is None
    assert await auth_manager.authenticate_with_token(token) is None


async def test_short_lived_jwt_exp_carries_absolute_max(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that a short-lived JWT's exp claim equals the absolute max lifetime.

    The database expires_at enforces the sliding idle window; an exp claim shorter
    than the absolute max would cut off active sessions before renewal can happen.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="jwtexpuser", role=UserRole.USER)
    token = await auth_manager.create_token(user, "JWT Exp Test", is_long_lived=False)

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None
    created_at = datetime.fromisoformat(token_row["created_at"])

    payload = auth_manager.jwt_helper.decode_token(token, verify_exp=False)
    expected = created_at + timedelta(days=TOKEN_ABSOLUTE_MAX_EXPIRATION)
    assert payload["exp"] == int(expected.timestamp())

    # The database keeps the shorter sliding window as source of truth
    expires_at = datetime.fromisoformat(token_row["expires_at"])
    assert expires_at - created_at == timedelta(days=TOKEN_SHORT_LIVED_EXPIRATION)


async def test_guest_token_fixed_short_lifetime(auth_manager: AuthenticationManager) -> None:
    """
    Test that guest tokens get a short fixed lifetime and never renew on use.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="guestexpiry", role=UserRole.GUEST)
    token = await auth_manager.create_token(user, "Guest Session", is_long_lived=False)

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert token_row is not None
    created_at = datetime.fromisoformat(token_row["created_at"])
    expires_at = datetime.fromisoformat(token_row["expires_at"])
    assert expires_at - created_at == timedelta(days=TOKEN_GUEST_EXPIRATION)

    # The JWT exp claim must match the fixed window, not the absolute max
    payload = auth_manager.jwt_helper.decode_token(token, verify_exp=False)
    assert payload["exp"] == int(expires_at.timestamp())

    # Authenticating must not extend the expiration (no sliding window for guests)
    assert await auth_manager.authenticate_with_token(token) is not None
    updated_row = await auth_manager.database.get_row("auth_tokens", {"token_hash": token_hash})
    assert updated_row is not None
    assert updated_row["expires_at"] == token_row["expires_at"]


async def test_guest_cannot_create_long_lived_token(auth_manager: AuthenticationManager) -> None:
    """
    Test that a guest cannot create a long-lived token for their own account.

    :param auth_manager: AuthenticationManager instance.
    """
    guest = await auth_manager.create_user(username="guesttoken", role=UserRole.GUEST)
    set_current_user(guest)

    with pytest.raises(InsufficientPermissions):
        await auth_manager.create_long_lived_token("Guest Escalation")


async def test_guest_cannot_remove_own_player_filter(
    auth_manager: AuthenticationManager,
) -> None:
    """A dedicated guest cannot make its own player access unrestricted."""
    guest = await auth_manager.create_user(
        username="filteredguest",
        role=UserRole.GUEST,
        player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID],
    )
    set_impersonated_user(None)
    set_current_user(guest)

    with pytest.raises(InsufficientPermissions):
        await auth_manager.update_user_profile(player_filter=[])

    stored_guest = await auth_manager.get_user(guest.user_id)
    assert stored_guest is not None
    assert stored_guest.player_filter == [GUEST_ACCESS_RESTRICTED_PLAYER_ID]


async def test_admin_can_update_guest_player_filter(
    auth_manager: AuthenticationManager,
) -> None:
    """An administrator may still manage a dedicated guest's player filter."""
    admin = await auth_manager.create_user(username="filteradmin", role=UserRole.ADMIN)
    guest = await auth_manager.create_user(
        username="managedguest",
        role=UserRole.GUEST,
        player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID],
    )
    set_impersonated_user(None)
    set_current_user(admin)

    with patch.object(auth_manager.webserver, "disconnect_websockets_for_user") as disconnect:
        updated_guest = await auth_manager.update_user_profile(
            user_id=guest.user_id,
            player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID, "party_queue"],
        )

    assert updated_guest.player_filter == [
        GUEST_ACCESS_RESTRICTED_PLAYER_ID,
        "party_queue",
    ]
    disconnect.assert_called_once_with(guest.user_id)


async def test_unchanged_user_filter_does_not_disconnect(
    auth_manager: AuthenticationManager,
) -> None:
    """An unchanged managed filter does not write or disconnect the user."""
    guest = await auth_manager.create_user(
        username="unchangedfilter",
        role=UserRole.GUEST,
        player_filter=[GUEST_ACCESS_RESTRICTED_PLAYER_ID],
    )

    with patch.object(auth_manager.webserver, "disconnect_websockets_for_user") as disconnect:
        result = await auth_manager.update_user_filters(
            guest,
            [GUEST_ACCESS_RESTRICTED_PLAYER_ID],
            None,
        )

    assert result is guest
    disconnect.assert_not_called()


async def test_no_long_lived_token_for_guest_account(auth_manager: AuthenticationManager) -> None:
    """
    Test that a long-lived token cannot be created for a guest account, even by an admin.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="tokenadmin", role=UserRole.ADMIN)
    guest = await auth_manager.create_user(username="guesttarget", role=UserRole.GUEST)
    set_current_user(admin)

    with pytest.raises(InsufficientPermissions):
        await auth_manager.create_long_lived_token("Guest Token", user_id=guest.user_id)


async def test_username_case_insensitive_creation(auth_manager: AuthenticationManager) -> None:
    """
    Test that usernames are normalized to lowercase on creation.

    :param auth_manager: AuthenticationManager instance.
    """
    # Create user with mixed case username
    user = await auth_manager.create_user(
        username="TestUser",
        role=UserRole.USER,
        display_name="Test User",
    )

    # Username should be stored in lowercase
    assert user.username == "testuser"


async def test_username_case_insensitive_duplicate_prevention(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that duplicate usernames with different cases are prevented.

    :param auth_manager: AuthenticationManager instance.
    """
    # Create user with lowercase username
    await auth_manager.create_user(username="admin", role=UserRole.USER)

    # Try to create user with same username but different case should fail
    # (SQLite UNIQUE constraint violation)
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        await auth_manager.create_user(username="Admin", role=UserRole.USER)


async def test_username_case_insensitive_login(auth_manager: AuthenticationManager) -> None:
    """
    Test that login works with any case variation of username.

    :param auth_manager: AuthenticationManager instance.
    """
    builtin_provider = auth_manager.login_providers.get("builtin")
    assert builtin_provider is not None
    assert isinstance(builtin_provider, BuiltinLoginProvider)

    # Create user with lowercase username
    await builtin_provider.create_user_with_password(
        username="testadmin",
        password="SecurePassword123",
        role=UserRole.ADMIN,
    )

    # Test login with lowercase
    result = await auth_manager.authenticate_with_credentials(
        "builtin",
        {"username": "testadmin", "password": "SecurePassword123"},
    )
    assert result.success is True
    assert result.user is not None
    assert result.user.username == "testadmin"

    # Test login with uppercase
    result = await auth_manager.authenticate_with_credentials(
        "builtin",
        {"username": "TESTADMIN", "password": "SecurePassword123"},
    )
    assert result.success is True
    assert result.user is not None
    assert result.user.username == "testadmin"

    # Test login with mixed case
    result = await auth_manager.authenticate_with_credentials(
        "builtin",
        {"username": "TestAdmin", "password": "SecurePassword123"},
    )
    assert result.success is True
    assert result.user is not None
    assert result.user.username == "testadmin"


async def test_username_case_insensitive_lookup(auth_manager: AuthenticationManager) -> None:
    """
    Test that user lookup by username is case-insensitive.

    :param auth_manager: AuthenticationManager instance.
    """
    # Create user with lowercase username
    created_user = await auth_manager.create_user(username="lookupuser", role=UserRole.USER)

    # Lookup with lowercase
    user1 = await auth_manager.get_user_by_username("lookupuser")
    assert user1 is not None
    assert user1.user_id == created_user.user_id

    # Lookup with uppercase
    user2 = await auth_manager.get_user_by_username("LOOKUPUSER")
    assert user2 is not None
    assert user2.user_id == created_user.user_id

    # Lookup with mixed case
    user3 = await auth_manager.get_user_by_username("LookUpUser")
    assert user3 is not None
    assert user3.user_id == created_user.user_id


async def test_username_update_normalizes(auth_manager: AuthenticationManager) -> None:
    """
    Test that updating username normalizes it to lowercase.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="originaluser", role=UserRole.USER)

    # Update username with mixed case
    updated_user = await auth_manager.update_user(user, username="UpdatedUser")

    # Username should be normalized to lowercase
    assert updated_user is not None
    assert updated_user.username == "updateduser"


async def test_link_user_to_provider_idempotent(auth_manager: AuthenticationManager) -> None:
    """
    Test that linking user to provider is idempotent.

    This tests the fix for the bug where re-linking a user would cause
    IntegrityError due to UNIQUE constraint on (provider_type, provider_user_id).

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="hauser", role=UserRole.USER)

    # Link user to Home Assistant provider for the first time
    link1 = await auth_manager.link_user_to_provider(
        user,
        AuthProviderType.HOME_ASSISTANT,
        "ha_user_456",
    )

    assert link1 is not None
    assert link1.user_id == user.user_id
    assert link1.provider_type == AuthProviderType.HOME_ASSISTANT
    assert link1.provider_user_id == "ha_user_456"

    # Linking the same user again should return existing link without error
    link2 = await auth_manager.link_user_to_provider(
        user,
        AuthProviderType.HOME_ASSISTANT,
        "ha_user_456",
    )

    assert link2 is not None
    assert link2.link_id == link1.link_id  # Should be same link
    assert link2.user_id == user.user_id
    assert link2.provider_type == AuthProviderType.HOME_ASSISTANT
    assert link2.provider_user_id == "ha_user_456"


async def test_ingress_auth_existing_username(auth_manager: AuthenticationManager) -> None:
    """
    Test HA ingress auth when username exists but isn't linked to HA provider.

    This tests the scenario where a user is created during setup, and then
    tries to login via HA ingress with the same username.

    :param auth_manager: AuthenticationManager instance.
    """
    # Simulate user created during initial setup
    existing_user = await auth_manager.create_user(
        username="admin",
        role=UserRole.ADMIN,
        display_name="Admin User",
    )

    # Now simulate HA ingress trying to auto-create a user with same username
    # This should find the existing user and link it instead of creating new one
    user = await auth_manager.get_user_by_username("admin")
    assert user is not None
    assert user.user_id == existing_user.user_id

    # Link the existing user to HA provider (what ingress flow would do)
    link = await auth_manager.link_user_to_provider(
        user,
        AuthProviderType.HOME_ASSISTANT,
        "ha_admin_123",
    )

    assert link is not None
    assert link.user_id == existing_user.user_id

    # Verify we can retrieve user by provider link
    retrieved_user = await auth_manager.get_user_by_provider_link(
        AuthProviderType.HOME_ASSISTANT,
        "ha_admin_123",
    )

    assert retrieved_user is not None
    assert retrieved_user.user_id == existing_user.user_id
    assert retrieved_user.username == "admin"


# ==================== Join Code Tests ====================


async def test_generate_join_code(auth_manager: AuthenticationManager) -> None:
    """
    Test generating a join code for a user.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="joincodeuser", role=UserRole.GUEST)

    code, expires_at = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
        max_uses=0,
        device_name="Test Device",
    )

    assert code is not None
    assert len(code) == JOIN_CODE_LENGTH
    assert code.isalnum()
    assert expires_at is not None
    assert expires_at > utc()


async def test_get_join_code_expiry(auth_manager: AuthenticationManager) -> None:
    """
    Test looking up the expiry for a specific active join code.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="joinexpiryuser", role=UserRole.GUEST)

    code, expires_at = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
    )

    assert await auth_manager.get_join_code_expiry(code, user) == expires_at
    assert await auth_manager.get_join_code_expiry(code.lower(), user) == expires_at
    assert await auth_manager.get_join_code_expiry("BADCODE", user) is None


async def test_get_join_code_expiry_requires_matching_user(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that join code expiry lookup can be scoped to a specific user.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="joinexpiryowner", role=UserRole.GUEST)
    other_user = await auth_manager.create_user(
        username="joinexpiryother",
        role=UserRole.GUEST,
    )

    code, expires_at = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
    )

    assert await auth_manager.get_join_code_expiry(code, user) == expires_at
    assert await auth_manager.get_join_code_expiry(code) == expires_at
    assert await auth_manager.get_join_code_expiry(code, other_user) is None


async def test_get_join_code_expiry_expired(auth_manager: AuthenticationManager) -> None:
    """
    Test that expired join codes have no active expiry.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="joinexpiryexpired", role=UserRole.GUEST)

    code, _ = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
    )
    code_row = await auth_manager.database.get_row("join_codes", {"code": code})
    assert code_row is not None

    past_time = utc() - timedelta(hours=1)
    await auth_manager.database.update(
        "join_codes",
        {"code_id": code_row["code_id"]},
        {"expires_at": past_time.isoformat()},
    )

    assert await auth_manager.get_join_code_expiry(code, user) is None


async def test_generate_join_code_non_guest_rejected(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test that generating a join code for non-guest users is rejected.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="joinadmin", role=UserRole.ADMIN)
    user = await auth_manager.create_user(username="joinuser", role=UserRole.USER)

    with pytest.raises(ValueError, match="guest accounts"):
        await auth_manager.generate_join_code(user=admin)

    with pytest.raises(ValueError, match="guest accounts"):
        await auth_manager.generate_join_code(user=user)


async def test_exchange_join_code(auth_manager: AuthenticationManager) -> None:
    """
    Test exchanging a valid join code for a JWT token.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="exchangeuser", role=UserRole.GUEST)

    code, _ = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
        device_name="Exchange Test",
    )

    # Exchange code for token
    token = await auth_manager._exchange_join_code(code)

    assert token is not None
    assert len(token) > 0

    # Verify token works for authentication
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is not None
    assert authenticated_user.user_id == user.user_id
    assert authenticated_user.username == user.username


async def test_exchange_join_code_case_insensitive(auth_manager: AuthenticationManager) -> None:
    """
    Test that join codes are case-insensitive.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="caseuser", role=UserRole.GUEST)

    code, _ = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
    )

    # Exchange with lowercase version
    token = await auth_manager._exchange_join_code(code.lower())
    assert token is not None

    # Verify token works
    authenticated_user = await auth_manager.authenticate_with_token(token)
    assert authenticated_user is not None
    assert authenticated_user.user_id == user.user_id


async def test_exchange_join_code_invalid(auth_manager: AuthenticationManager) -> None:
    """
    Test that invalid join codes are rejected.

    :param auth_manager: AuthenticationManager instance.
    """
    token = await auth_manager._exchange_join_code("INVALID")
    assert token is None


async def test_exchange_join_code_expired(auth_manager: AuthenticationManager) -> None:
    """
    Test that expired join codes are rejected.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="expiredcodeuser", role=UserRole.GUEST)

    code, _ = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
    )

    # Manually expire the code by updating expires_at in database
    code_row = await auth_manager.database.get_row("join_codes", {"code": code})
    assert code_row is not None

    past_time = utc() - timedelta(hours=1)
    await auth_manager.database.update(
        "join_codes",
        {"code_id": code_row["code_id"]},
        {"expires_at": past_time.isoformat()},
    )

    # Try to exchange expired code
    token = await auth_manager._exchange_join_code(code)
    assert token is None


async def test_exchange_join_code_max_uses(auth_manager: AuthenticationManager) -> None:
    """
    Test that join codes respect max_uses limit.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="maxusesuser", role=UserRole.GUEST)

    code, _ = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
        max_uses=2,  # Only allow 2 uses
    )

    # First use should succeed
    token1 = await auth_manager._exchange_join_code(code)
    assert token1 is not None

    # Second use should succeed
    token2 = await auth_manager._exchange_join_code(code)
    assert token2 is not None

    # Third use should fail (max_uses=2 exceeded)
    token3 = await auth_manager._exchange_join_code(code)
    assert token3 is None


async def test_exchange_join_code_unlimited_uses(auth_manager: AuthenticationManager) -> None:
    """
    Test that join codes with max_uses=0 have unlimited uses.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="unlimiteduser", role=UserRole.GUEST)

    code, _ = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
        max_uses=0,  # Unlimited
    )

    # Should be able to use multiple times
    for _ in range(5):
        token = await auth_manager._exchange_join_code(code)
        assert token is not None


async def test_revoke_join_codes_for_user(auth_manager: AuthenticationManager) -> None:
    """
    Test revoking join codes for a specific user.

    :param auth_manager: AuthenticationManager instance.
    """
    user1 = await auth_manager.create_user(username="revokeuser1", role=UserRole.GUEST)
    user2 = await auth_manager.create_user(username="revokeuser2", role=UserRole.GUEST)

    # Create codes for both users
    code1, _ = await auth_manager.generate_join_code(user=user1)
    code2, _ = await auth_manager.generate_join_code(user=user2)

    # Revoke codes for user1 only
    revoked_count = await auth_manager.revoke_join_codes(user1)
    assert revoked_count == 1

    # User1's code should no longer work
    token1 = await auth_manager._exchange_join_code(code1)
    assert token1 is None

    # User2's code should still work
    token2 = await auth_manager._exchange_join_code(code2)
    assert token2 is not None


async def test_authenticate_with_join_code_api(auth_manager: AuthenticationManager) -> None:
    """
    Test the public API endpoint for join code authentication.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(
        username="apijoincodeuser",
        role=UserRole.GUEST,
        display_name="API Guest",
    )

    code, _ = await auth_manager.generate_join_code(
        user=user,
        expires_in_hours=24,
    )

    # Call the API endpoint
    result = await auth_manager.exchange_join_code(code)

    assert result["success"] is True
    assert "access_token" in result
    assert result["user"]["user_id"] == user.user_id
    assert result["user"]["username"] == user.username
    assert result["user"]["role"] == "guest"


async def test_authenticate_with_join_code_api_invalid(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Test the API endpoint with invalid join code.

    :param auth_manager: AuthenticationManager instance.
    """
    result = await auth_manager.exchange_join_code("BADCODE")

    assert result["success"] is False
    assert "error" in result
    assert "access_token" not in result


async def test_list_join_codes(auth_manager: AuthenticationManager) -> None:
    """
    Test listing active join codes (admin only).

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="listcodesadmin", role=UserRole.ADMIN)
    guest1 = await auth_manager.create_user(username="listguest1", role=UserRole.GUEST)
    guest2 = await auth_manager.create_user(username="listguest2", role=UserRole.GUEST)
    set_current_user(admin)

    # Create codes for both guests
    await auth_manager.generate_join_code(user=guest1)
    await auth_manager.generate_join_code(user=guest2)

    # List all codes
    codes = await auth_manager.list_join_codes()
    assert len(codes) == 2

    # List codes for specific user
    codes = await auth_manager.list_join_codes(user_id=guest1.user_id)
    assert len(codes) == 1
    assert codes[0]["user_id"] == guest1.user_id


async def test_revoke_join_code_api(auth_manager: AuthenticationManager) -> None:
    """
    Test revoking a specific join code by code_id (admin only).

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="revokecodeadmin", role=UserRole.ADMIN)
    guest = await auth_manager.create_user(username="revokeguest", role=UserRole.GUEST)
    set_current_user(admin)

    code, _ = await auth_manager.generate_join_code(user=guest)

    # Get the code_id from the database
    codes = await auth_manager.list_join_codes(user_id=guest.user_id)
    assert len(codes) == 1
    code_id = codes[0]["code_id"]

    # Revoke the specific code
    await auth_manager.revoke_join_code(code_id)

    # Code should no longer work
    token = await auth_manager._exchange_join_code(code)
    assert token is None

    # List should be empty
    codes = await auth_manager.list_join_codes(user_id=guest.user_id)
    assert len(codes) == 0


async def test_revoke_join_code_api_not_found(auth_manager: AuthenticationManager) -> None:
    """
    Test revoking a non-existent join code raises error.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="revokenotfound", role=UserRole.ADMIN)
    set_current_user(admin)

    with pytest.raises(InvalidDataError, match="Join code not found"):
        await auth_manager.revoke_join_code("nonexistent-code-id")


async def test_impersonated_user_context_manager(auth_manager: AuthenticationManager) -> None:
    """Test the ImpersonatedUser context manager."""
    admin_user = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    standard_user_a = await auth_manager.create_user(username="user_a", role=UserRole.USER)
    standard_user_b = await auth_manager.create_user(username="user_b", role=UserRole.USER)
    service_user = await auth_manager.create_user(username="service", role=UserRole.SERVICE)

    # non-authenticated user must raise
    set_current_user(None)
    with pytest.raises(InsufficientPermissions):
        async with ImpersonatedUser(auth_manager.mass, "user_a"):
            ...
    # impersonation attempt without the users.impersonate scope must raise
    set_current_user(standard_user_a)
    with pytest.raises(InsufficientPermissions):
        async with ImpersonatedUser(auth_manager.mass, "admin"):
            ...
    # invalid username must raise
    set_current_user(admin_user)
    with pytest.raises(InvalidDataError):
        async with ImpersonatedUser(auth_manager.mass, "wrong_username"):
            ...

    # verify that a standard user may impersonate itself (by username or user_id)
    set_current_user(standard_user_a)
    set_impersonated_user(None)
    async with ImpersonatedUser(auth_manager.mass, "user_a"):
        assert get_current_user() == standard_user_a
    async with ImpersonatedUser(auth_manager.mass, standard_user_a.user_id):
        assert get_current_user() == standard_user_a
    # passing None is a no-op which preserves any active impersonation
    set_impersonated_user(standard_user_b)
    async with ImpersonatedUser(auth_manager.mass, None):
        assert get_current_user() == standard_user_b
    assert get_current_user() == standard_user_b

    # verify that an admin user may impersonate another user
    set_current_user(admin_user)

    set_impersonated_user(None)  # non-nested use
    assert get_current_user() == admin_user
    async with ImpersonatedUser(auth_manager.mass, "user_a"):
        assert get_current_user() == standard_user_a
    assert get_current_user() == admin_user

    set_impersonated_user(standard_user_b)  # nested use
    async with ImpersonatedUser(auth_manager.mass, "user_a"):
        assert get_current_user() == standard_user_a
    assert get_current_user() == standard_user_b

    # verify that a service user may impersonate another user (users.impersonate scope)
    set_current_user(service_user)
    set_impersonated_user(None)
    assert has_scope(service_user, Scope.USERS_IMPERSONATE)
    async with ImpersonatedUser(auth_manager.mass, "user_a"):
        assert get_current_user() == standard_user_a
    assert get_current_user() == service_user


async def test_impersonated_user_anonymous_playback_is_noop(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Verify an unauthenticated call without a username is a no-op.

    Regression: play_media wraps every call in ImpersonatedUser, so protocol/hardware
    triggered playback (presets, Spotify Connect, ...) - which has no authenticated user
    and passes no username - must not raise.
    """
    set_current_user(None)
    set_impersonated_user(None)
    async with ImpersonatedUser(auth_manager.mass, None):
        assert get_current_user() is None
    assert get_current_user() is None

    # an unauthenticated caller may still not impersonate another user
    with pytest.raises(InsufficientPermissions):
        async with ImpersonatedUser(auth_manager.mass, "user_a"):
            ...


async def test_join_code_length_at_least_12() -> None:
    """Verify join codes are long enough to resist brute force (security finding 7.3.2)."""
    assert JOIN_CODE_LENGTH >= 12


async def test_exchange_join_code_rate_limited(auth_manager: AuthenticationManager) -> None:
    """
    Verify repeated failed join code exchanges get throttled (security finding 7.3.2).

    :param auth_manager: AuthenticationManager instance.
    """
    # Three failures trip the progressive delay threshold.
    for _ in range(3):
        result = await auth_manager.exchange_join_code("WRONGCODE123")
        assert result["success"] is False

    # The next attempt must be rejected for rate limiting, not just "invalid".
    result = await auth_manager.exchange_join_code("WRONGCODE123")
    assert result["success"] is False
    assert "too many" in result["error"].lower()


async def test_exchange_join_code_rate_limit_concurrent_burst(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Verify concurrent failed exchanges cannot race past the rate limiter.

    Without serialization, parallel requests all pass the rate limit check
    before any of them records a failure, allowing brute-force bursts.

    :param auth_manager: AuthenticationManager instance.
    """
    results = await asyncio.gather(
        *(auth_manager.exchange_join_code("WRONGCODE123") for _ in range(10))
    )

    assert all(result["success"] is False for result in results)
    # Only the first 3 attempts may reach the actual code check; the rest must be throttled.
    invalid_count = sum(1 for result in results if "invalid" in result["error"].lower())
    throttled_count = sum(1 for result in results if "too many" in result["error"].lower())
    assert invalid_count == 3
    assert throttled_count == 7


async def test_exchange_join_code_success_does_not_reset_rate_limit(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Verify a successful exchange does not clear the failed-attempt counter.

    The rate limit key is global, so clearing on success would let an attacker
    holding any valid code reset the counter at will and bypass throttling.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="norstuser", role=UserRole.GUEST)
    code, _ = await auth_manager.generate_join_code(user=user, expires_in_hours=24, max_uses=0)

    # A couple of failures, still below the throttle threshold.
    for _ in range(2):
        assert (await auth_manager.exchange_join_code("NOPE12345678"))["success"] is False

    # A valid exchange still succeeds but must not wipe the counter.
    assert (await auth_manager.exchange_join_code(code))["success"] is True

    # The third failure trips the threshold, throttling further attempts.
    assert (await auth_manager.exchange_join_code("NOPE12345678"))["success"] is False
    result = await auth_manager.exchange_join_code(code)
    assert result["success"] is False
    assert "too many" in result["error"].lower()


async def test_resolve_command_impersonation(auth_manager: AuthenticationManager) -> None:
    """Test resolving the impersonation argument of an incoming API command."""
    admin_user = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    standard_user = await auth_manager.create_user(username="user_a", role=UserRole.USER)
    set_current_user(admin_user)
    set_impersonated_user(None)

    # no user argument present is a no-op and leaves other args untouched
    args: dict[str, object] = {"queue_id": "abc"}
    assert await resolve_command_impersonation(auth_manager.mass, args) is None
    assert args == {"queue_id": "abc"}

    # an empty string is deliberately treated as "no impersonation requested"
    # (optional fields in automations/scripts commonly template to an empty string)
    args = {"queue_id": "abc", "user": ""}
    assert await resolve_command_impersonation(auth_manager.mass, args) is None
    assert args == {"queue_id": "abc"}

    # the user argument is popped and resolved (by username)
    args = {"queue_id": "abc", "user": "user_a"}
    resolved = await resolve_command_impersonation(auth_manager.mass, args)
    assert resolved == standard_user
    assert args == {"queue_id": "abc"}

    # the user argument is also resolved by user_id
    args = {"user": standard_user.user_id}
    resolved = await resolve_command_impersonation(auth_manager.mass, args)
    assert resolved == standard_user

    # username is accepted as (deprecated) alias for user
    args = {"username": "user_a"}
    resolved = await resolve_command_impersonation(auth_manager.mass, args)
    assert resolved == standard_user
    assert args == {}

    # a caller without the users.impersonate scope may not impersonate another user
    set_current_user(standard_user)
    with pytest.raises(InsufficientPermissions):
        await resolve_command_impersonation(auth_manager.mass, {"user": "admin"})


def test_has_scope() -> None:
    """Test the scope check for each of the builtin user roles."""

    def _user(role: str) -> User:
        return User(user_id="abc123", username="testuser", role=role)

    # admin has all scopes through the wildcard
    assert has_scope(_user(UserRole.ADMIN), Scope.CONFIG_CORE_WRITE)
    assert has_scope(_user(UserRole.ADMIN), Scope.LIBRARY_MANAGE)
    # regular user
    assert has_scope(_user(UserRole.USER), Scope.LIBRARY_WRITE)
    assert has_scope(_user(UserRole.USER), Scope.CONFIG_CORE_READ)
    assert not has_scope(_user(UserRole.USER), Scope.CONFIG_CORE_WRITE)
    assert not has_scope(_user(UserRole.USER), Scope.USERS_IMPERSONATE)
    # guest
    assert has_scope(_user(UserRole.GUEST), Scope.LIBRARY_READ)
    assert not has_scope(_user(UserRole.GUEST), Scope.LIBRARY_WRITE)
    assert not has_scope(_user(UserRole.GUEST), Scope.CONFIG_CORE_READ)
    # service
    assert has_scope(_user(UserRole.SERVICE), Scope.USERS_IMPERSONATE)
    assert has_scope(_user(UserRole.SERVICE), Scope.CONFIG_PLAYERS_WRITE)
    assert not has_scope(_user(UserRole.SERVICE), Scope.CONFIG_CORE_WRITE)
    # an unknown (custom) role id is fail-closed and grants no scopes at all
    assert not has_scope(_user("some_future_role"), Scope.LIBRARY_READ)


async def test_homeassistant_system_user_has_service_role(
    auth_manager: AuthenticationManager,
) -> None:
    """Test that the Home Assistant system user is created with the service role."""
    system_user = await auth_manager.get_homeassistant_system_user()
    assert system_user.role == UserRole.SERVICE

    # a pre-existing system user with the old user role is migrated to service
    await auth_manager.database.update(
        "users", {"user_id": system_user.user_id}, {"role": UserRole.USER.value}
    )
    await auth_manager._migrate_system_user_role()
    migrated_user = await auth_manager.get_user(system_user.user_id)
    assert migrated_user is not None
    assert migrated_user.role == UserRole.SERVICE
