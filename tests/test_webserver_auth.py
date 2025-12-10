"""Tests for webserver authentication and user management."""

import asyncio
import hashlib
import logging
import pathlib
from collections.abc import AsyncGenerator
from datetime import timedelta
from sqlite3 import IntegrityError

import pytest
from music_assistant_models.auth import AuthProviderType, UserRole
from music_assistant_models.errors import InvalidDataError

from music_assistant.constants import HOMEASSISTANT_SYSTEM_USER
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.webserver.auth import AuthenticationManager
from music_assistant.controllers.webserver.controller import WebserverController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    set_current_token,
    set_current_user,
)
from music_assistant.controllers.webserver.helpers.auth_providers import BuiltinLoginProvider
from music_assistant.helpers.datetime import utc
from music_assistant.mass import MusicAssistant


@pytest.fixture
async def mass_minimal(tmp_path: pathlib.Path) -> AsyncGenerator[MusicAssistant, None]:
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


async def test_homeassistant_system_user_token(auth_manager: AuthenticationManager) -> None:
    """Test Home Assistant system user token creation.

    :param auth_manager: AuthenticationManager instance.
    """
    # Get or create token
    token1 = await auth_manager.get_homeassistant_system_user_token()
    assert token1 is not None

    # Getting it again should create a new token (old one is replaced)
    token2 = await auth_manager.get_homeassistant_system_user_token()
    assert token2 is not None
    assert token2 != token1

    # Old token should not work
    user1 = await auth_manager.authenticate_with_token(token1)
    assert user1 is None

    # New token should work
    user2 = await auth_manager.authenticate_with_token(token2)
    assert user2 is not None


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
