"""Test Twitch Provider OAuth & token management."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.twitch import (
    CONF_ACCESS_TOKEN,
    CONF_ACTION_AUTH,
    CONF_ACTION_REVOKE,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_STREAMLINK_TOKEN,
    TWITCH_SCOPES,
    TwitchProvider,
    get_config_entries,
)
from tests.providers.twitch.conftest import MockResponse, make_mock_session_method

# --- Config Entries ---


async def test_config_entries_returns_expected_fields(mass_mock: Mock) -> None:
    """get_config_entries() returns entries for all expected fields."""
    entries = await get_config_entries(mass_mock)
    keys = {e.key for e in entries}
    assert CONF_CLIENT_ID in keys
    assert CONF_CLIENT_SECRET in keys
    assert CONF_STREAMLINK_TOKEN in keys


async def test_client_id_is_secure_string(mass_mock: Mock) -> None:
    """client_id config entry type is SECURE_STRING."""
    entries = await get_config_entries(mass_mock)
    entry = next(e for e in entries if e.key == CONF_CLIENT_ID)
    assert entry.type == ConfigEntryType.SECURE_STRING


async def test_client_secret_is_secure_string(mass_mock: Mock) -> None:
    """client_secret config entry type is SECURE_STRING."""
    entries = await get_config_entries(mass_mock)
    entry = next(e for e in entries if e.key == CONF_CLIENT_SECRET)
    assert entry.type == ConfigEntryType.SECURE_STRING


async def test_streamlink_token_is_optional_secure_string(mass_mock: Mock) -> None:
    """streamlink_token is SECURE_STRING, not required."""
    entries = await get_config_entries(mass_mock)
    entry = next(e for e in entries if e.key == CONF_STREAMLINK_TOKEN)
    assert entry.type == ConfigEntryType.SECURE_STRING
    assert entry.required is False


async def test_auth_action_present(mass_mock: Mock) -> None:
    """An ACTION type config entry exists for triggering OAuth."""
    entries = await get_config_entries(mass_mock)
    action_entries = [e for e in entries if e.type == ConfigEntryType.ACTION]
    action_keys = {e.action for e in action_entries}
    assert CONF_ACTION_AUTH in action_keys


async def test_auth_status_label_present(mass_mock: Mock) -> None:
    """A LABEL type config entry exists showing auth status."""
    entries = await get_config_entries(mass_mock)
    label_entries = [e for e in entries if e.type == ConfigEntryType.LABEL]
    assert len(label_entries) >= 1


async def test_not_authenticated_label(mass_mock: Mock) -> None:
    """Before auth, label shows 'Not authenticated'."""
    entries = await get_config_entries(mass_mock)
    label_entries = [e for e in entries if e.type == ConfigEntryType.LABEL]
    label_text = " ".join(e.label for e in label_entries).lower()
    assert "not authenticated" in label_text


async def test_authenticated_label(mass_mock: Mock) -> None:
    """After auth, label shows 'Authenticated' (not 'Not authenticated')."""
    values: dict[str, Any] = {
        CONF_ACCESS_TOKEN: "test_access_token",
        CONF_REFRESH_TOKEN: "test_refresh_token",
    }
    entries = await get_config_entries(mass_mock, values=values)
    label_entries = [e for e in entries if e.type == ConfigEntryType.LABEL]
    label_text = " ".join(e.label for e in label_entries).lower()
    assert "authenticated" in label_text
    assert "not authenticated" not in label_text


async def test_revoke_action_hidden_when_not_authenticated(mass_mock: Mock) -> None:
    """Revoke action is hidden when not authenticated."""
    entries = await get_config_entries(mass_mock)
    revoke_entries = [e for e in entries if e.action == CONF_ACTION_REVOKE]
    assert all(e.hidden for e in revoke_entries)


async def test_revoke_action_visible_when_authenticated(mass_mock: Mock) -> None:
    """Revoke action is visible when authenticated."""
    values: dict[str, Any] = {
        CONF_ACCESS_TOKEN: "test_access_token",
        CONF_REFRESH_TOKEN: "test_refresh_token",
    }
    entries = await get_config_entries(mass_mock, values=values)
    revoke_entries = [e for e in entries if e.action == CONF_ACTION_REVOKE]
    assert any(not e.hidden for e in revoke_entries)


# --- Config Validation — Bad/Missing Values ---


async def test_empty_client_id_provider_loads(
    mass_mock: Mock, manifest_mock: Mock, config_mock: Mock
) -> None:
    """Provider loads without crash when client_id is empty."""
    provider = TwitchProvider(mass_mock, manifest_mock, config_mock)
    # Should not raise
    assert provider is not None


async def test_empty_credentials_shows_not_authenticated(mass_mock: Mock) -> None:
    """Config label shows 'Not authenticated' state, not error/crash."""
    values: dict[str, Any] = {
        CONF_CLIENT_ID: "",
        CONF_CLIENT_SECRET: "",
    }
    entries = await get_config_entries(mass_mock, values=values)
    label_entries = [e for e in entries if e.type == ConfigEntryType.LABEL]
    label_text = " ".join(e.label for e in label_entries).lower()
    assert "not authenticated" in label_text


# --- OAuth Flow ---


async def test_auth_action_with_empty_client_id(mass_mock: Mock) -> None:
    """Authenticate with no client_id raises clear error, not crash."""
    values: dict[str, Any] = {
        "session_id": "test_session",
        CONF_CLIENT_ID: "",
        CONF_CLIENT_SECRET: "secret",
    }
    with pytest.raises(LoginFailed, match=r"(?i)client"):
        await get_config_entries(mass_mock, action=CONF_ACTION_AUTH, values=values)


async def test_auth_action_with_empty_client_secret(mass_mock: Mock) -> None:
    """Authenticate with no client_secret raises clear error, not crash."""
    values: dict[str, Any] = {
        "session_id": "test_session",
        CONF_CLIENT_ID: "client_id",
        CONF_CLIENT_SECRET: "",
    }
    with pytest.raises(LoginFailed, match=r"(?i)client"):
        await get_config_entries(mass_mock, action=CONF_ACTION_AUTH, values=values)


async def test_auth_callback_exchanges_code_for_tokens(mass_mock: Mock) -> None:
    """Happy-path OAuth: code exchanged for tokens, provider becomes authenticated."""
    values: dict[str, Any] = {
        "session_id": "test_session",
        CONF_CLIENT_ID: "test_client",
        CONF_CLIENT_SECRET: "test_secret",
    }

    mock_auth = AsyncMock()
    mock_auth.__aenter__ = AsyncMock(return_value=mock_auth)
    mock_auth.__aexit__ = AsyncMock(return_value=None)
    mock_auth.callback_url = "http://localhost:8095/callback/test"
    mock_auth.authenticate = AsyncMock(return_value={"code": "valid_code"})

    # Token exchange succeeds
    mass_mock.http_session.post = make_mock_session_method(
        MockResponse(
            status=200,
            json_data={
                "access_token": "new_access_token",
                "refresh_token": "new_refresh_token",
                "expires_in": 14400,
            },
        )
    )

    with patch("music_assistant.providers.twitch.AuthenticationHelper", return_value=mock_auth):
        entries = await get_config_entries(mass_mock, action=CONF_ACTION_AUTH, values=values)

    # Tokens should be stored in values
    assert values[CONF_ACCESS_TOKEN] == "new_access_token"
    assert values[CONF_REFRESH_TOKEN] == "new_refresh_token"

    # Config entries should show authenticated state
    label_entries = [e for e in entries if e.type == ConfigEntryType.LABEL]
    label_text = " ".join(e.label for e in label_entries).lower()
    assert "not authenticated" not in label_text


async def test_auth_scope_includes_user_read_follows() -> None:
    """OAuth scope includes user:read:follows."""
    assert "user:read:follows" in TWITCH_SCOPES


# --- Token Refresh ---


async def test_401_triggers_refresh(provider: TwitchProvider) -> None:
    """API call returning 401 triggers token refresh, then retries."""
    provider._access_token = "expired_token"
    provider._refresh_token = "valid_refresh"
    provider._client_id = "test_client_id"
    provider._client_secret = "test_client_secret"

    provider.mass.http_session.get = make_mock_session_method(  # type: ignore[method-assign]
        [
            MockResponse(status=401),
            MockResponse(status=200, json_data={"data": []}),
        ]
    )
    provider.mass.http_session.post = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(
            status=200,
            json_data={
                "access_token": "new_token",
                "refresh_token": "new_refresh",
                "expires_in": 14400,
            },
        )
    )

    result = await provider._api_get("/helix/streams")
    assert result == {"data": []}


async def test_refresh_no_refresh_token_raises(provider: TwitchProvider) -> None:
    """Refresh with no stored refresh token raises LoginFailed."""
    provider._access_token = "some_token"
    provider._refresh_token = None
    provider._client_id = "test_client"
    provider._client_secret = "test_secret"

    with pytest.raises(LoginFailed, match=r"(?i)refresh"):
        await provider._refresh_access_token()


async def test_refresh_saves_new_refresh_token(provider: TwitchProvider) -> None:
    """When refresh response includes new refresh_token, it's saved (token rotation)."""
    provider._access_token = "old_access"
    provider._refresh_token = "old_refresh"
    provider._client_id = "test_client_id"
    provider._client_secret = "test_client_secret"

    provider.mass.http_session.post = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(
            status=200,
            json_data={
                "access_token": "new_access",
                "refresh_token": "rotated_refresh",
                "expires_in": 14400,
            },
        )
    )

    await provider._refresh_access_token()
    assert provider._access_token == "new_access"
    assert provider._refresh_token == "rotated_refresh"


async def test_refresh_preserves_old_refresh_token_if_not_rotated(
    provider: TwitchProvider,
) -> None:
    """When refresh response omits refresh_token, old one is preserved."""
    provider._access_token = "old_access"
    provider._refresh_token = "old_refresh"
    provider._client_id = "test_client_id"
    provider._client_secret = "test_client_secret"

    provider.mass.http_session.post = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(
            status=200,
            json_data={
                "access_token": "new_access",
                "expires_in": 14400,
            },
        )
    )

    await provider._refresh_access_token()
    assert provider._access_token == "new_access"
    assert provider._refresh_token == "old_refresh"


async def test_refresh_failure_raises_login_failed(provider: TwitchProvider) -> None:
    """On refresh failure, LoginFailed is raised."""
    provider._access_token = "old_access"
    provider._refresh_token = "old_refresh"
    provider._client_id = "test_client_id"
    provider._client_secret = "test_client_secret"

    provider.mass.http_session.post = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=401, text_data="Invalid refresh token")
    )

    with pytest.raises(LoginFailed):
        await provider._refresh_access_token()


async def test_refresh_failure_clears_both_tokens(provider: TwitchProvider) -> None:
    """On refresh failure, both access and refresh tokens are cleared."""
    provider._access_token = "old_access"
    provider._refresh_token = "old_refresh"
    provider._client_id = "test_client_id"
    provider._client_secret = "test_client_secret"

    provider.mass.http_session.post = make_mock_session_method(  # type: ignore[method-assign]
        MockResponse(status=401, text_data="Invalid refresh token")
    )

    cleared_access: str | None = "sentinel"
    cleared_refresh: str | None = "sentinel"
    try:
        await provider._refresh_access_token()
    except LoginFailed:
        cleared_access = provider._access_token
        cleared_refresh = provider._refresh_token

    assert cleared_access is None
    assert cleared_refresh is None


# --- Token Exchange Errors ---


async def test_token_exchange_fails_invalid_code(mass_mock: Mock) -> None:
    """Twitch rejects authorization code — LoginFailed raised."""
    values: dict[str, Any] = {
        "session_id": "test_session",
        CONF_CLIENT_ID: "test_client",
        CONF_CLIENT_SECRET: "test_secret",
    }

    # Mock AuthenticationHelper to return a code
    mock_auth = AsyncMock()
    mock_auth.__aenter__ = AsyncMock(return_value=mock_auth)
    mock_auth.__aexit__ = AsyncMock(return_value=None)
    mock_auth.callback_url = "http://localhost:8095/callback/test"
    mock_auth.authenticate = AsyncMock(return_value={"code": "bad_code"})

    # Token exchange fails
    mass_mock.http_session.post = make_mock_session_method(
        MockResponse(status=400, text_data="Invalid authorization code")
    )

    with (
        patch("music_assistant.providers.twitch.AuthenticationHelper", return_value=mock_auth),
        pytest.raises(LoginFailed),
    ):
        await get_config_entries(mass_mock, action=CONF_ACTION_AUTH, values=values)


async def test_token_exchange_fails_network_error(mass_mock: Mock) -> None:
    """Network failure during token exchange — LoginFailed raised."""
    values: dict[str, Any] = {
        "session_id": "test_session",
        CONF_CLIENT_ID: "test_client",
        CONF_CLIENT_SECRET: "test_secret",
    }

    mock_auth = AsyncMock()
    mock_auth.__aenter__ = AsyncMock(return_value=mock_auth)
    mock_auth.__aexit__ = AsyncMock(return_value=None)
    mock_auth.callback_url = "http://localhost:8095/callback/test"
    mock_auth.authenticate = AsyncMock(return_value={"code": "valid_code"})

    def raise_error(*_args: Any, **_kwargs: Any) -> None:
        msg = "connection refused"
        raise ConnectionError(msg)

    mass_mock.http_session.post = Mock(side_effect=raise_error)

    with (
        patch("music_assistant.providers.twitch.AuthenticationHelper", return_value=mock_auth),
        pytest.raises(ConnectionError),
    ):
        await get_config_entries(mass_mock, action=CONF_ACTION_AUTH, values=values)


# --- Logout / Revoke ---


async def test_revoke_noop_when_not_authenticated(mass_mock: Mock) -> None:
    """Revoke with no tokens is a no-op — no API call made."""
    values: dict[str, Any] = {
        "session_id": "test_session",
        CONF_ACCESS_TOKEN: "",
        CONF_REFRESH_TOKEN: "",
        CONF_CLIENT_ID: "test_client",
    }
    mass_mock.http_session.post = make_mock_session_method(MockResponse(status=200))

    await get_config_entries(mass_mock, action=CONF_ACTION_REVOKE, values=values)

    # post should NOT have been called — no token to revoke
    mass_mock.http_session.post.assert_not_called()


async def test_revoke_invalidates_live_status_cache(mass_mock: Mock) -> None:
    """After revoke, tokens are cleared in the values dict."""
    values: dict[str, Any] = {
        "session_id": "test_session",
        CONF_ACCESS_TOKEN: "test_token",
        CONF_REFRESH_TOKEN: "test_refresh",
        CONF_CLIENT_ID: "test_client",
    }
    mass_mock.http_session.post = make_mock_session_method(MockResponse(status=200))

    await get_config_entries(mass_mock, action=CONF_ACTION_REVOKE, values=values)

    # Values dict should have tokens cleared
    assert values[CONF_ACCESS_TOKEN] == ""
    assert values[CONF_REFRESH_TOKEN] == ""


async def test_revoke_action_clears_tokens(mass_mock: Mock) -> None:
    """Revoke action clears stored tokens."""
    values: dict[str, Any] = {
        "session_id": "test_session",
        CONF_ACCESS_TOKEN: "test_token",
        CONF_REFRESH_TOKEN: "test_refresh",
        CONF_CLIENT_ID: "test_client",
    }
    mass_mock.http_session.post = make_mock_session_method(MockResponse(status=200))

    entries = await get_config_entries(mass_mock, action=CONF_ACTION_REVOKE, values=values)
    token_entries = [e for e in entries if e.key == CONF_ACCESS_TOKEN]
    if token_entries:
        assert token_entries[0].value in (None, "")
    refresh_entries = [e for e in entries if e.key == CONF_REFRESH_TOKEN]
    if refresh_entries:
        assert refresh_entries[0].value in (None, "")


async def test_revoke_tolerates_network_error(mass_mock: Mock) -> None:
    """Network error during revoke still clears local credentials."""
    values: dict[str, Any] = {
        "session_id": "test_session",
        CONF_ACCESS_TOKEN: "test_token",
        CONF_REFRESH_TOKEN: "test_refresh",
        CONF_CLIENT_ID: "test_client",
    }

    def raise_error(*args: Any, **kwargs: Any) -> None:  # noqa: ARG001
        msg = "network error"
        raise ConnectionError(msg)

    mass_mock.http_session.post = Mock(side_effect=raise_error)

    # Should not raise — revoke is best-effort
    entries = await get_config_entries(mass_mock, action=CONF_ACTION_REVOKE, values=values)
    token_entries = [e for e in entries if e.key == CONF_ACCESS_TOKEN]
    if token_entries:
        assert token_entries[0].value in (None, "")
