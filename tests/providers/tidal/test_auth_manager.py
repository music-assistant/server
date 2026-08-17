"""Test Tidal Auth Manager."""

import json
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientSession
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.tidal.auth_manager import TidalAuthManager


@pytest.fixture
def http_session() -> AsyncMock:
    """Return a mock http session."""
    return AsyncMock(spec=ClientSession)


@pytest.fixture
def config_updater() -> Mock:
    """Return a mock config updater."""
    return Mock()


@pytest.fixture
def auth_manager(http_session: AsyncMock, config_updater: Mock) -> TidalAuthManager:
    """Return a TidalAuthManager instance."""
    logger = Mock()
    return TidalAuthManager(http_session, config_updater, logger)


async def test_initialize_success(auth_manager: TidalAuthManager) -> None:
    """Test successful initialization."""
    auth_data = json.dumps(
        {
            "access_token": "token",
            "refresh_token": "refresh",
            "expires_at": time.time() + 3600,
        }
    )
    assert await auth_manager.initialize(auth_data) is True
    assert auth_manager.access_token == "token"


async def test_initialize_invalid_json(auth_manager: TidalAuthManager) -> None:
    """Test initialization with invalid JSON."""
    assert await auth_manager.initialize("invalid") is False


async def test_ensure_valid_token_valid(auth_manager: TidalAuthManager) -> None:
    """Test ensure_valid_token with valid token."""
    auth_manager._auth_info = {"expires_at": time.time() + 3600}
    assert await auth_manager.ensure_valid_token() is True


async def test_ensure_valid_token_expired(
    auth_manager: TidalAuthManager, http_session: AsyncMock, config_updater: Mock
) -> None:
    """Test ensure_valid_token with expired token."""
    auth_manager._auth_info = {
        "expires_at": time.time() - 3600,
        "refresh_token": "refresh",
    }

    # Mock refresh response
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {
        "access_token": "new_token",
        "expires_in": 3600,
        "refresh_token": "new_refresh",
    }
    http_session.post.return_value.__aenter__.return_value = response

    assert await auth_manager.ensure_valid_token() is True
    assert auth_manager.access_token == "new_token"
    config_updater.assert_called_once()


async def test_refresh_token_failure(
    auth_manager: TidalAuthManager, http_session: AsyncMock
) -> None:
    """Test refresh_token failure."""
    auth_manager._auth_info = {
        "refresh_token": "refresh",
    }

    # Mock refresh response failure
    response = AsyncMock()
    response.status = 400
    response.text.return_value = "Bad Request"
    http_session.post.return_value.__aenter__.return_value = response

    assert await auth_manager.refresh_token() is False


async def test_refresh_token_cooldown(
    auth_manager: TidalAuthManager, http_session: AsyncMock
) -> None:
    """Test a refresh right after a successful one skips the token endpoint."""
    auth_manager._auth_info = {
        "refresh_token": "refresh",
    }

    response = AsyncMock()
    response.status = 200
    response.json.return_value = {
        "access_token": "new_token",
        "expires_in": 3600,
    }
    http_session.post.return_value.__aenter__.return_value = response

    assert await auth_manager.refresh_token() is True
    assert await auth_manager.refresh_token() is True
    assert http_session.post.call_count == 1


@patch("music_assistant.providers.tidal.auth_manager.app_var")
async def test_start_device_login(mock_app_var: Mock, http_session: AsyncMock) -> None:
    """start_device_login returns the device authorization response."""
    mock_app_var.side_effect = ["client_id", "client_secret"]
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {
        "deviceCode": "dev",
        "userCode": "ABCDE",
        "verificationUri": "link.tidal.com",
        "interval": 2,
        "expiresIn": 300,
    }
    http_session.post.return_value.__aenter__.return_value = response

    device = await TidalAuthManager.start_device_login(http_session)

    assert device["deviceCode"] == "dev"
    assert device["userCode"] == "ABCDE"


@patch("music_assistant.providers.tidal.auth_manager.app_var")
async def test_start_device_login_failure(mock_app_var: Mock, http_session: AsyncMock) -> None:
    """start_device_login raises when the client is rejected (e.g. wrong client type)."""
    mock_app_var.side_effect = ["client_id", "client_secret"]
    response = AsyncMock()
    response.status = 400
    response.json.return_value = {"error": "invalid_request"}
    http_session.post.return_value.__aenter__.return_value = response

    with pytest.raises(LoginFailed):
        await TidalAuthManager.start_device_login(http_session)


@patch("music_assistant.providers.tidal.auth_manager.app_var")
async def test_start_device_login_non_json_body(
    mock_app_var: Mock, http_session: AsyncMock
) -> None:
    """A non-JSON body (e.g. proxy HTML error page) surfaces as LoginFailed."""
    mock_app_var.side_effect = ["client_id", "client_secret"]
    response = AsyncMock()
    response.status = 502
    response.json.side_effect = json.JSONDecodeError("Expecting value", "<html>", 0)
    http_session.post.return_value.__aenter__.return_value = response

    with pytest.raises(LoginFailed, match="non-JSON"):
        await TidalAuthManager.start_device_login(http_session)


@patch("music_assistant.providers.tidal.auth_manager.app_var")
async def test_poll_device_login_success(mock_app_var: Mock, http_session: AsyncMock) -> None:
    """poll_device_login returns auth data with user info and an absolute expiry."""
    mock_app_var.side_effect = ["client_id", "client_secret"]
    device = {"deviceCode": "dev", "interval": 0}

    token_response = AsyncMock()
    token_response.status = 200
    token_response.json.return_value = {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_in": 3600,
    }
    user_response = AsyncMock()
    user_response.status = 200
    user_response.json.return_value = {"userId": "user_id", "countryCode": "US"}

    http_session.post.return_value.__aenter__.return_value = token_response
    http_session.get.return_value.__aenter__.return_value = user_response

    result = await TidalAuthManager.poll_device_login(http_session, device)

    assert result["access_token"] == "access"
    assert result["userId"] == "user_id"
    assert result["expires_at"] > time.time()


@patch("music_assistant.providers.tidal.auth_manager.app_var")
async def test_poll_device_login_waits_for_pending(
    mock_app_var: Mock, http_session: AsyncMock
) -> None:
    """poll_device_login keeps polling while authorization is pending, then succeeds."""
    mock_app_var.side_effect = ["client_id", "client_secret"]
    device = {"deviceCode": "dev", "interval": 0}

    pending = AsyncMock()
    pending.status = 400
    pending.json.return_value = {"error": "authorization_pending"}
    ok = AsyncMock()
    ok.status = 200
    ok.json.return_value = {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_in": 3600,
    }
    user_response = AsyncMock()
    user_response.status = 200
    user_response.json.return_value = {"userId": "user_id"}

    http_session.post.return_value.__aenter__.side_effect = [pending, ok]
    http_session.get.return_value.__aenter__.return_value = user_response

    result = await TidalAuthManager.poll_device_login(http_session, device)

    assert result["access_token"] == "access"
    assert http_session.post.call_count == 2


@patch("music_assistant.providers.tidal.auth_manager.app_var")
async def test_poll_device_login_terminal_error(
    mock_app_var: Mock, http_session: AsyncMock
) -> None:
    """poll_device_login raises on a terminal (non-pending) error such as denial."""
    mock_app_var.side_effect = ["client_id", "client_secret"]
    device = {"deviceCode": "dev", "interval": 0}

    response = AsyncMock()
    response.status = 400
    response.json.return_value = {"error": "access_denied"}
    http_session.post.return_value.__aenter__.return_value = response

    with pytest.raises(LoginFailed):
        await TidalAuthManager.poll_device_login(http_session, device)


async def test_finalize_login_missing_tokens(http_session: AsyncMock) -> None:
    """_finalize_login raises when the token response lacks the required tokens."""
    with pytest.raises(LoginFailed):
        await TidalAuthManager._finalize_login(http_session, {"access_token": "only_access"})


async def test_finalize_login_sessions_error(http_session: AsyncMock) -> None:
    """_finalize_login raises when the sessions lookup returns a non-200."""
    response = AsyncMock()
    response.status = 500
    response.text.return_value = "server error"
    http_session.get.return_value.__aenter__.return_value = response

    with pytest.raises(LoginFailed):
        await TidalAuthManager._finalize_login(
            http_session, {"access_token": "access", "refresh_token": "refresh"}
        )


@patch("music_assistant.providers.tidal.auth_manager.asyncio.sleep")
@patch("music_assistant.providers.tidal.auth_manager.app_var")
async def test_poll_device_login_expired_token_keeps_polling(
    mock_app_var: Mock, mock_sleep: AsyncMock, http_session: AsyncMock
) -> None:
    """
    An expired_token response keeps polling; the step deadline re-mints the code.

    Tidal's clock may declare the code expired moments before the setup flow's own
    deadline fires, so treating it as terminal would abort a flow about to recover.
    """
    mock_app_var.side_effect = ["client_id", "client_secret"]
    device = {"deviceCode": "dev", "interval": 0}

    expired = AsyncMock()
    expired.status = 400
    expired.json.return_value = {"error": "expired_token"}
    ok = AsyncMock()
    ok.status = 200
    ok.json.return_value = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    user = AsyncMock()
    user.status = 200
    user.json.return_value = {"userId": "u"}

    http_session.post.return_value.__aenter__.side_effect = [expired, ok]
    http_session.get.return_value.__aenter__.return_value = user

    result = await TidalAuthManager.poll_device_login(http_session, device)

    assert result["access_token"] == "a"
    assert http_session.post.call_count == 2
    assert mock_sleep.await_count == 2


@patch("music_assistant.providers.tidal.auth_manager.asyncio.sleep")
@patch("music_assistant.providers.tidal.auth_manager.app_var")
async def test_poll_device_login_survives_gateway_error(
    mock_app_var: Mock, mock_sleep: AsyncMock, http_session: AsyncMock
) -> None:
    """A transient 5xx mid-poll keeps polling instead of aborting the login."""
    mock_app_var.side_effect = ["client_id", "client_secret"]
    device = {"deviceCode": "dev", "interval": 0}

    gateway_error = AsyncMock()
    gateway_error.status = 502
    gateway_error.json.side_effect = json.JSONDecodeError("Expecting value", "<html>", 0)
    ok = AsyncMock()
    ok.status = 200
    ok.json.return_value = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    user = AsyncMock()
    user.status = 200
    user.json.return_value = {"userId": "u"}

    http_session.post.return_value.__aenter__.side_effect = [gateway_error, ok]
    http_session.get.return_value.__aenter__.return_value = user

    result = await TidalAuthManager.poll_device_login(http_session, device)

    assert result["access_token"] == "a"
    assert http_session.post.call_count == 2
    # slept before each poll: the 502 waited for the next interval, no tight retry
    assert mock_sleep.await_count == 2


@patch("music_assistant.providers.tidal.auth_manager.asyncio.sleep")
@patch("music_assistant.providers.tidal.auth_manager.app_var")
async def test_poll_device_login_slow_down(
    mock_app_var: Mock, mock_sleep: AsyncMock, http_session: AsyncMock
) -> None:
    """poll_device_login backs off on slow_down and then succeeds."""
    mock_app_var.side_effect = ["client_id", "client_secret"]
    device = {"deviceCode": "dev", "interval": 0}

    slow = AsyncMock()
    slow.status = 400
    slow.json.return_value = {"error": "slow_down"}
    ok = AsyncMock()
    ok.status = 200
    ok.json.return_value = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    user = AsyncMock()
    user.status = 200
    user.json.return_value = {"userId": "u"}

    http_session.post.return_value.__aenter__.side_effect = [slow, ok]
    http_session.get.return_value.__aenter__.return_value = user

    result = await TidalAuthManager.poll_device_login(http_session, device)

    assert result["access_token"] == "a"
    assert http_session.post.call_count == 2
    # slept once before each poll (the second interval was bumped by slow_down)
    assert mock_sleep.await_count == 2
