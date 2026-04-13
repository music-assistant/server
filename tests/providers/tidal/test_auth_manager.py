"""Test Tidal Auth Manager."""

import json
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientSession
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.tidal.auth_manager import (
    ManualAuthenticationHelper,
    TidalAuthManager,
)


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
            "client_id": "client_id",
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
        "client_id": "client_id",
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
        "client_id": "client_id",
    }

    # Mock refresh response failure
    response = AsyncMock()
    response.status = 400
    response.text.return_value = "Bad Request"
    http_session.post.return_value.__aenter__.return_value = response

    assert await auth_manager.refresh_token() is False


@patch("music_assistant.providers.tidal.auth_manager.pkce")
@patch("music_assistant.providers.tidal.auth_manager.app_var")
@pytest.mark.usefixtures("auth_manager")
async def test_generate_auth_url(mock_app_var: Mock, mock_pkce: Mock) -> None:
    """Test generate_auth_url."""
    mock_pkce.generate_pkce_pair.return_value = ("verifier", "challenge")
    mock_app_var.side_effect = ["client_id", "client_secret"]

    mass = Mock()
    mass.loop.call_soon_threadsafe = Mock()
    auth_helper = ManualAuthenticationHelper(mass, "session_id")

    result = await TidalAuthManager.generate_auth_url(auth_helper, "HIGH")

    assert "code_verifier" in result
    assert "client_unique_key" in result
    mass.loop.call_soon_threadsafe.assert_called_once()


async def test_process_pkce_login_success(http_session: AsyncMock) -> None:
    """Test process_pkce_login success."""
    auth_params = json.dumps(
        {
            "code_verifier": "verifier",
            "client_unique_key": "key",
            "client_id": "id",
            "client_secret": "secret",
            "quality": "HIGH",
        }
    )
    redirect_url = "https://tidal.com/android/login/auth?code=auth_code"

    # Mock token response
    token_response = AsyncMock()
    token_response.status = 200
    token_response.json.return_value = {
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_in": 3600,
    }

    # Mock user info response
    user_response = AsyncMock()
    user_response.status = 200
    user_response.json.return_value = {
        "id": "user_id",
        "username": "user",
    }

    http_session.post.return_value.__aenter__.return_value = token_response
    http_session.get.return_value.__aenter__.return_value = user_response

    result = await TidalAuthManager.process_pkce_login(http_session, auth_params, redirect_url)

    assert result["access_token"] == "access"
    assert result["id"] == "user_id"


async def test_process_pkce_login_missing_code(http_session: AsyncMock) -> None:
    """Test process_pkce_login missing code."""
    auth_params = json.dumps(
        {
            "code_verifier": "verifier",
            "client_unique_key": "key",
        }
    )
    redirect_url = "https://tidal.com/android/login/auth"

    with pytest.raises(LoginFailed, match="No authorization code"):
        await TidalAuthManager.process_pkce_login(http_session, auth_params, redirect_url)
