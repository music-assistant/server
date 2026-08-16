"""Tests for the OneDrive OAuth helper (token refresh and rotation)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError

from music_assistant.providers.filesystem_cloud.base import CONF_REFRESH_TOKEN
from music_assistant.providers.filesystem_onedrive.auth import MAOneDriveAuth, _friendly_auth_error


def _make_auth() -> tuple[MAOneDriveAuth, MagicMock]:
    """Return an auth helper plus the mocked mass behind it."""
    mass = MagicMock()
    # the store keeps values encrypted; encrypt is an identity map for the test
    mass.config.encrypt_string = MagicMock(side_effect=lambda value: value)
    return MAOneDriveAuth(mass, "onedrive--test", "client-id", "client-secret", "refresh-1"), mass


def _response_cm(response: MagicMock) -> MagicMock:
    """Build a fake async context manager mimicking aiohttp's session.post()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _token_response(payload: dict[str, object]) -> MagicMock:
    """Build a fake 200 token response with the given JSON payload."""
    response = MagicMock()
    response.status = 200
    response.json = AsyncMock(return_value=payload)
    return response


async def test_cached_access_token_is_reused() -> None:
    """A still-valid access token is returned without calling Microsoft."""
    auth, mass = _make_auth()
    auth._access_token = "cached-token"
    auth._expires_at = time.time() + 3600

    assert await auth.async_get_access_token() == "cached-token"
    mass.http_session.post.assert_not_called()


async def test_refresh_persists_rotated_token_immediately() -> None:
    """A rotated refresh token is stored encrypted, bypassing the debounced save."""
    auth, mass = _make_auth()
    mass.http_session.post = MagicMock(
        return_value=_response_cm(
            _token_response(
                {"access_token": "access-1", "expires_in": 3600, "refresh_token": "refresh-2"}
            )
        )
    )

    assert await auth.async_get_access_token() == "access-1"

    assert auth._refresh_token == "refresh-2"
    # the rotated token is persisted into setup_data (encrypt is identity here)
    mass.config.set.assert_called_once_with(
        f"providers/onedrive--test/setup_data/{CONF_REFRESH_TOKEN}", "refresh-2", immediate=True
    )


async def test_refresh_skips_persisting_unchanged_token() -> None:
    """No setup_data write happens when Microsoft returns the same refresh token."""
    auth, mass = _make_auth()
    mass.http_session.post = MagicMock(
        return_value=_response_cm(
            _token_response(
                {"access_token": "access-1", "expires_in": 3600, "refresh_token": "refresh-1"}
            )
        )
    )

    await auth.async_get_access_token()

    mass.config.set.assert_not_called()


async def test_refresh_keeps_token_when_none_returned() -> None:
    """A response without a refresh token keeps the current one."""
    auth, mass = _make_auth()
    mass.http_session.post = MagicMock(
        return_value=_response_cm(_token_response({"access_token": "access-1", "expires_in": 60}))
    )

    await auth.async_get_access_token()

    assert auth._refresh_token == "refresh-1"
    mass.config.set.assert_not_called()


async def test_refresh_auth_error_raises_login_failed() -> None:
    """A 400 from the token endpoint surfaces as LoginFailed with a friendly message."""
    auth, mass = _make_auth()
    response = MagicMock()
    response.status = 400
    response.text = AsyncMock(return_value='{"error": "invalid_grant"}')
    mass.http_session.post = MagicMock(return_value=_response_cm(response))

    with pytest.raises(LoginFailed, match="re-authorize"):
        await auth.async_get_access_token()


async def test_refresh_network_error_raises_provider_unavailable() -> None:
    """Transport failures are not treated as auth problems."""
    auth, mass = _make_auth()
    mass.http_session.post = MagicMock(side_effect=ClientError("connection reset"))

    with pytest.raises(ProviderUnavailableError):
        await auth.async_get_access_token()


def test_friendly_auth_error_mapping() -> None:
    """Well-known Microsoft error codes map to actionable messages."""
    assert "secret has expired" in str(_friendly_auth_error("AADSTS7000222: expired"))
    assert "Secret ID" in str(_friendly_auth_error("AADSTS7000215: invalid secret"))
    assert "re-authorize" in str(_friendly_auth_error("invalid_grant: revoked"))
    assert _friendly_auth_error("something else entirely") is None
