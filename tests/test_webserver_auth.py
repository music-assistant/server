"""Tests for webserver authentication and user management."""

import asyncio
import hashlib
import logging
import pathlib
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from sqlite3 import IntegrityError

import pytest
from music_assistant_models.auth import AuthProviderType, UserRole
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.webserver.auth import (
    JOIN_CODE_COOLDOWN_SECONDS,
    JOIN_CODE_FAILURE_CEILING,
    JOIN_CODE_LENGTH,
    TOKEN_ABSOLUTE_MAX_EXPIRATION,
    TOKEN_GUEST_EXPIRATION,
    TOKEN_LONG_LIVED_EXPIRATION,
    TOKEN_SHORT_LIVED_EXPIRATION,
    AuthenticationManager,
)
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    is_system_user_allowed_admin_command,
    resolve_username_workaround,
    set_current_token,
    set_current_user,
)
from music_assistant.controllers.webserver.helpers.auth_providers import (
    DEFAULT_LOGIN_DELAY_TIERS,
    BuiltinLoginProvider,
    LoginRateLimiter,
)
from music_assistant.helpers.datetime import utc
from music_assistant.mass import MusicAssistant


@pytest.fixture
async def mass_minimal(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant]:
    """Create a minimal Music Assistant instance for auth testing without starting the webserver.

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
    # Use id() as fallback since _thread_id is a private attribute that may not exist
    mass_instance.loop_thread_id = (
        getattr(mass_instance.loop, "_thread_id", None)
        if hasattr(mass_instance.loop, "_thread_id")
        else id(mass_instance.loop)
    )

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
    """Get authentication manager from mass instance.

    :param mass_minimal: Minimal MusicAssistant instance.
    """
    return mass_minimal.webserver.auth


async def test_auth_manager_initialization(auth_manager: AuthenticationManager) -> None:
    """Test that the authentication manager initializes correctly.

    :param auth_manager: AuthenticationManager instance.
    """
    assert auth_manager is not None
    assert auth_manager.database is not None
    assert "builtin" in auth_manager.login_providers
    assert isinstance(auth_manager.login_providers["builtin"], BuiltinLoginProvider)


async def test_has_users_initially_empty(auth_manager: AuthenticationManager) -> None:
    """Test that has_users returns False when no users exist.

    :param auth_manager: AuthenticationManager instance.
    """
    has_users = auth_manager.has_users
    assert has_users is False


async def test_create_user(auth_manager: AuthenticationManager) -> None:
    """Test creating a new user.

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
    """Test retrieving a user by ID.

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
    """Test creating a user with built-in authentication.

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
    """Test authenticating with username and password.

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
    """Test creating access tokens.

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
    """Test authenticating with an access token.

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
    """Test that expired tokens are rejected.

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
    """Test updating user profile information.

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
    """Test changing user password.

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
    """Test revoking an access token.

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
    """Test listing all users (admin only).

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
    """Test disabling and enabling user accounts.

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
    """Test that users cannot disable their own account.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="selfadmin", role=UserRole.ADMIN)
    set_current_user(admin)

    # Try to disable own account
    with pytest.raises(InvalidDataError, match="Cannot disable your own account"):
        await auth_manager.disable_user(admin.user_id)


async def test_user_preferences(auth_manager: AuthenticationManager) -> None:
    """Test updating user preferences.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="prefuser", role=UserRole.USER)

    # Update preferences
    preferences = {"theme": "dark", "language": "en"}
    updated_user = await auth_manager.update_user_preferences(user, preferences)

    assert updated_user is not None
    assert updated_user.preferences == preferences


async def test_link_user_to_provider(auth_manager: AuthenticationManager) -> None:
    """Test linking user to authentication provider.

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
    """Test Home Assistant system user creation.

    :param auth_manager: AuthenticationManager instance.
    """
    # Get or create system user
    system_user = await auth_manager.get_homeassistant_system_user()

    assert system_user is not None
    assert system_user.username == HOMEASSISTANT_SYSTEM_USER
    assert system_user.display_name == "Home Assistant Integration"
    assert system_user.role == UserRole.USER

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
    """Test updating user role (admin only).

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
    """Test deleting a user account.

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
    """Test that users cannot delete their own account.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="selfdeleteadmin", role=UserRole.ADMIN)
    set_current_user(admin)

    # Try to delete own account
    with pytest.raises(InvalidDataError, match="Cannot delete your own account"):
        await auth_manager.delete_user(admin.user_id)


async def test_get_user_tokens(auth_manager: AuthenticationManager) -> None:
    """Test getting user's tokens.

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
    """Test getting available login providers.

    :param auth_manager: AuthenticationManager instance.
    """
    providers = await auth_manager.get_login_providers()

    assert len(providers) > 0
    assert any(p["provider_id"] == "builtin" for p in providers)


async def test_create_user_with_api(auth_manager: AuthenticationManager) -> None:
    """Test creating user via API command.

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
    """Test validation in create_user_with_api.

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
    """Test logout functionality.

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
    """Test that short-lived tokens auto-renew on use.

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
    """Test that long-lived tokens do NOT auto-renew on use.

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


async def test_username_case_insensitive_creation(auth_manager: AuthenticationManager) -> None:
    """Test that usernames are normalized to lowercase on creation.

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
    """Test that duplicate usernames with different cases are prevented.

    :param auth_manager: AuthenticationManager instance.
    """
    # Create user with lowercase username
    await auth_manager.create_user(username="admin", role=UserRole.USER)

    # Try to create user with same username but different case should fail
    # (SQLite UNIQUE constraint violation)
    with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
        await auth_manager.create_user(username="Admin", role=UserRole.USER)


async def test_username_case_insensitive_login(auth_manager: AuthenticationManager) -> None:
    """Test that login works with any case variation of username.

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
    """Test that user lookup by username is case-insensitive.

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
    """Test that updating username normalizes it to lowercase.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="originaluser", role=UserRole.USER)

    # Update username with mixed case
    updated_user = await auth_manager.update_user(user, username="UpdatedUser")

    # Username should be normalized to lowercase
    assert updated_user is not None
    assert updated_user.username == "updateduser"


async def test_link_user_to_provider_idempotent(auth_manager: AuthenticationManager) -> None:
    """Test that linking user to provider is idempotent.

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
    """Test HA ingress auth when username exists but isn't linked to HA provider.

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
    """Test generating a join code for a user.

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


async def test_generate_join_code_non_guest_rejected(
    auth_manager: AuthenticationManager,
) -> None:
    """Test that generating a join code for non-guest users is rejected.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="joinadmin", role=UserRole.ADMIN)
    user = await auth_manager.create_user(username="joinuser", role=UserRole.USER)

    with pytest.raises(ValueError, match="guest accounts"):
        await auth_manager.generate_join_code(user=admin)

    with pytest.raises(ValueError, match="guest accounts"):
        await auth_manager.generate_join_code(user=user)


async def test_exchange_join_code(auth_manager: AuthenticationManager) -> None:
    """Test exchanging a valid join code for a JWT token.

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
    """Test that join codes are case-insensitive.

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
    """Test that invalid join codes are rejected.

    :param auth_manager: AuthenticationManager instance.
    """
    token = await auth_manager._exchange_join_code("INVALID")
    assert token is None


async def test_exchange_join_code_expired(auth_manager: AuthenticationManager) -> None:
    """Test that expired join codes are rejected.

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
    """Test that join codes respect max_uses limit.

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
    """Test that join codes with max_uses=0 have unlimited uses.

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
    """Test revoking join codes for a specific user.

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
    """Test the public API endpoint for join code authentication.

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
    """Test the API endpoint with invalid join code.

    :param auth_manager: AuthenticationManager instance.
    """
    result = await auth_manager.exchange_join_code("BADCODE")

    assert result["success"] is False
    assert "error" in result
    assert "access_token" not in result


async def test_list_join_codes(auth_manager: AuthenticationManager) -> None:
    """Test listing active join codes (admin only).

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
    """Test revoking a specific join code by code_id (admin only).

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
    """Test revoking a non-existent join code raises error.

    :param auth_manager: AuthenticationManager instance.
    """
    admin = await auth_manager.create_user(username="revokenotfound", role=UserRole.ADMIN)
    set_current_user(admin)

    with pytest.raises(InvalidDataError, match="Join code not found"):
        await auth_manager.revoke_join_code("nonexistent-code-id")


async def test_system_user_allowed_admin_commands(auth_manager: AuthenticationManager) -> None:
    """
    Test the temporary stable-branch exemption for the Home Assistant system user.

    :param auth_manager: AuthenticationManager instance.
    """
    system_user = await auth_manager.get_homeassistant_system_user()
    standard_user = await auth_manager.create_user(username="user_a", role=UserRole.USER)

    assert is_system_user_allowed_admin_command(system_user, "players/remove")
    assert is_system_user_allowed_admin_command(system_user, "config/players/remove")
    # the integration lists users to resolve the calling Home Assistant user
    assert is_system_user_allowed_admin_command(system_user, "auth/users")
    # other admin commands remain off limits for the system user
    assert not is_system_user_allowed_admin_command(system_user, "config/core/save")
    # regular users are not exempt
    assert not is_system_user_allowed_admin_command(standard_user, "auth/users")
    assert not is_system_user_allowed_admin_command(standard_user, "players/remove")


async def test_username_workaround(auth_manager: AuthenticationManager) -> None:
    """
    Test the temporary stable-branch username argument on listing commands.

    :param auth_manager: AuthenticationManager instance.
    """
    mass = auth_manager.mass
    system_user = await auth_manager.get_homeassistant_system_user()
    admin_user = await auth_manager.create_user(username="admin", role=UserRole.ADMIN)
    standard_user = await auth_manager.create_user(username="user_a", role=UserRole.USER)

    # no username argument present is a no-op and leaves other args untouched
    set_current_user(system_user)
    args: dict[str, str | int] = {"limit": 10}
    await resolve_username_workaround(mass, "music/tracks/library_items", args)
    assert args == {"limit": 10}
    assert get_current_user() == system_user

    # the system user may execute listing commands as another user
    args = {"limit": 10, "username": "user_a"}
    await resolve_username_workaround(mass, "music/tracks/library_items", args)
    assert args == {"limit": 10}
    assert get_current_user() == standard_user

    # admin users may do the same (also on music/search)
    set_current_user(admin_user)
    await resolve_username_workaround(mass, "music/search", {"username": "user_a"})
    assert get_current_user() == standard_user

    # the username argument is ignored on other commands
    set_current_user(system_user)
    args = {"username": "user_a"}
    await resolve_username_workaround(mass, "player_queues/items", args)
    assert args == {"username": "user_a"}
    assert get_current_user() == system_user

    # an unknown username must raise
    with pytest.raises(InvalidDataError):
        await resolve_username_workaround(mass, "music/search", {"username": "nobody"})

    # a regular user may not execute listing commands as another user
    set_current_user(standard_user)
    with pytest.raises(InsufficientPermissions):
        await resolve_username_workaround(mass, "music/search", {"username": "admin"})
    # but passing their own username is a no-op
    await resolve_username_workaround(mass, "music/search", {"username": "user_a"})
    assert get_current_user() == standard_user


async def test_join_code_length_at_least_12() -> None:
    """Verify join codes are long enough to resist brute force (security finding 7.3.2)."""
    assert JOIN_CODE_LENGTH >= 12


async def test_exchange_join_code_party_burst_not_throttled(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Verify a party-scale burst of bad codes does not throttle the shared bucket.

    All guests share one rate limit key, so a handful of mistyped or stale codes must
    never lock out the guests holding a valid one.

    :param auth_manager: AuthenticationManager instance.
    """
    user = await auth_manager.create_user(username="partyguest", role=UserRole.GUEST)
    code, _ = await auth_manager.generate_join_code(user=user, expires_in_hours=24, max_uses=0)

    for _ in range(30):
        result = await auth_manager.exchange_join_code("WRONGCODE123")
        assert result["success"] is False
        assert "invalid" in result["error"].lower()

    # A guest with a valid code still gets in.
    assert (await auth_manager.exchange_join_code(code))["success"] is True


async def test_exchange_join_code_rate_limited_at_ceiling(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Verify failed join code exchanges are still throttled once the ceiling is reached.

    :param auth_manager: AuthenticationManager instance.
    """
    _lower_join_code_ceiling(auth_manager)

    for _ in range(3):
        result = await auth_manager.exchange_join_code("WRONGCODE123")
        assert result["success"] is False

    # The next attempt must be rejected for rate limiting, not just "invalid".
    result = await auth_manager.exchange_join_code("WRONGCODE123")
    assert result["success"] is False
    assert "too many" in result["error"].lower()


async def test_join_code_ceiling_far_above_party_scale() -> None:
    """Verify the shipped ceiling leaves room for a large party's worth of failures."""
    # 100 devices retrying 5 times each must stay well inside the ceiling.
    assert JOIN_CODE_FAILURE_CEILING >= 2 * 100 * 5


async def test_exchange_join_code_rate_limit_concurrent_burst(
    auth_manager: AuthenticationManager,
) -> None:
    """
    Verify concurrent failed exchanges cannot race past the rate limiter.

    Without serialization, parallel requests all pass the rate limit check
    before any of them records a failure, allowing brute-force bursts.

    :param auth_manager: AuthenticationManager instance.
    """
    _lower_join_code_ceiling(auth_manager)

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

    The rate limit key is shared by every caller, so clearing on success would let an
    attacker holding any valid code reset the counter at will and bypass throttling.

    :param auth_manager: AuthenticationManager instance.
    """
    _lower_join_code_ceiling(auth_manager)
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


async def test_login_rate_limiter_default_tiers_unchanged() -> None:
    """Verify the interactive login path keeps its progressive delays."""
    limiter = LoginRateLimiter()

    expected = {2: 0, 3: 30, 5: 30, 6: 60, 9: 60, 10: 120, 14: 120, 15: 300, 40: 300}
    recorded = 0
    for count, delay in sorted(expected.items()):
        while recorded < count:
            await limiter.record_failed_attempt("someuser")
            recorded += 1
        assert limiter.get_delay("someuser") == delay


async def test_login_rate_limiter_single_tier_is_a_flat_ceiling() -> None:
    """Verify a one-tier limiter stays free below the ceiling, then applies a flat delay."""
    limiter = LoginRateLimiter(delay_tiers=((4, 60),))

    for _ in range(3):
        await limiter.record_failed_attempt("shared")
        assert limiter.get_delay("shared") == 0

    await limiter.record_failed_attempt("shared")
    assert limiter.get_delay("shared") == 60
    await limiter.record_failed_attempt("shared")
    assert limiter.get_delay("shared") == 60


async def test_default_login_delay_tiers_ascending() -> None:
    """Verify the default tiers are ordered, since get_delay takes the highest match."""
    counts = [count for count, _ in DEFAULT_LOGIN_DELAY_TIERS]
    delays = [delay for _, delay in DEFAULT_LOGIN_DELAY_TIERS]
    assert counts == sorted(counts)
    assert delays == sorted(delays)


def _lower_join_code_ceiling(auth_manager: AuthenticationManager, ceiling: int = 3) -> None:
    """
    Swap in a join code rate limiter with a low ceiling so throttling is reachable in a test.

    :param auth_manager: AuthenticationManager instance to patch.
    :param ceiling: Failure count at which the cooldown starts applying.
    """
    auth_manager._join_code_rate_limiter = LoginRateLimiter(
        delay_tiers=((ceiling, JOIN_CODE_COOLDOWN_SECONDS),),
        warn_threshold=ceiling,
        alert_threshold=ceiling * 2,
        subject="rate limit key",
    )
