"""Tests for refreshing the short-lived Soundcloud access token."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.soundcloud import (
    CONF_AUTHORIZATION,
    CONF_REFRESH_TOKEN,
    TOKEN_REFRESH_BUFFER,
    TOKEN_URL,
    _token_claims,
)

if TYPE_CHECKING:
    from music_assistant.providers.soundcloud import SoundcloudMusicProvider

NOW = 1700000000


def _jwt(**claims: Any) -> str:
    """Build an unsigned JWT carrying the given claims."""

    def _part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{_part({'alg': 'none'})}.{_part(claims)}.signature"


def _token_response(status: int = 200, payload: dict[str, Any] | None = None) -> MagicMock:
    """Build the async context manager returned by http_session.post."""
    response = Mock()
    response.status = status
    response.json = AsyncMock(return_value=payload or {})
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _setup_values(provider: SoundcloudMusicProvider, **values: str) -> None:
    """Make the setup flow answer with the given values."""
    provider.get_setup_value = Mock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: values.get(key, default)
    )


def _api_with_token(auth_token: str) -> AsyncMock:
    """Build an api client that carries the given authorization."""
    api = AsyncMock()
    api.client_id = "test-client-id"
    api.headers = {"Authorization": auth_token}
    setattr(api, "o_auth", auth_token)  # noqa: B010
    api.get_account_details.return_value = {"id": 42}
    return api


@pytest.fixture
def stored(provider: SoundcloudMusicProvider) -> dict[str, Any]:
    """Capture what the provider persists to its setup data."""
    data: dict[str, Any] = {}

    def _store(key: str, value: Any, immediate: bool = True) -> None:
        assert immediate
        data[key] = value

    provider._update_setup_data = Mock(side_effect=_store)  # type: ignore[method-assign]
    return data


def test_token_claims_reads_jwt() -> None:
    """The claims of a JWT access token are read, with or without the OAuth prefix."""
    token = _jwt(exp=NOW, client_id="web-client")

    assert _token_claims(f"OAuth {token}") == {"exp": NOW, "client_id": "web-client"}
    assert _token_claims(token)["exp"] == NOW


@pytest.mark.parametrize("auth_token", ["OAuth 2-123456-789-AbCdEf", "", "OAuth a.b.c"])
def test_token_claims_of_opaque_token(auth_token: str) -> None:
    """A legacy opaque token (or garbage) has no claims, so it is never refreshed."""
    assert _token_claims(auth_token) == {}


async def test_refresh_rotates_and_persists_tokens(
    provider: SoundcloudMusicProvider, stored: dict[str, Any]
) -> None:
    """A refresh swaps the api authorization and stores both rotated tokens."""
    _setup_values(provider, refresh_token="old-refresh")
    provider._soundcloud = _api_with_token(f"OAuth {_jwt(exp=NOW, client_id='web-client')}")
    new_auth = f"OAuth {_jwt(exp=NOW + 3600)}"
    post = Mock(
        return_value=_token_response(
            payload={"access_token": new_auth.removeprefix("OAuth "), "refresh_token": "new"}
        )
    )

    with patch.object(provider.mass.http_session, "post", post):
        assert await provider._refresh_access_token() == new_auth

    post.assert_called_once_with(
        TOKEN_URL,
        data={
            "client_id": "web-client",
            "grant_type": "refresh_token",
            "refresh_token": "old-refresh",
        },
    )
    assert provider._soundcloud.headers["Authorization"] == new_auth
    assert provider._soundcloud.o_auth == new_auth
    assert stored == {CONF_REFRESH_TOKEN: "new", CONF_AUTHORIZATION: new_auth}


async def test_refresh_falls_back_to_configured_client_id(
    provider: SoundcloudMusicProvider, stored: dict[str, Any]
) -> None:
    """Without a client_id claim the configured client id is used."""
    _setup_values(provider, refresh_token="old-refresh")
    provider._soundcloud = _api_with_token(f"OAuth {_jwt(exp=NOW)}")
    post = Mock(return_value=_token_response(payload={"access_token": _jwt(exp=NOW + 3600)}))

    with patch.object(provider.mass.http_session, "post", post):
        await provider._refresh_access_token()

    assert post.call_args.kwargs["data"]["client_id"] == "test-client-id"
    # no rotated refresh token was handed out, so the stored one is kept
    assert CONF_REFRESH_TOKEN not in stored


async def test_refresh_without_refresh_token_fails(provider: SoundcloudMusicProvider) -> None:
    """An expired token without a refresh token asks the user to sign in again."""
    _setup_values(provider)
    provider._soundcloud = _api_with_token(f"OAuth {_jwt(exp=NOW)}")

    with pytest.raises(LoginFailed):
        await provider._refresh_access_token()


async def test_refresh_rejected_fails(provider: SoundcloudMusicProvider) -> None:
    """A refresh token Soundcloud no longer accepts is a login failure."""
    _setup_values(provider, refresh_token="revoked")
    provider._soundcloud = _api_with_token(f"OAuth {_jwt(exp=NOW)}")
    post = Mock(return_value=_token_response(status=400))

    with patch.object(provider.mass.http_session, "post", post), pytest.raises(LoginFailed):
        await provider._refresh_access_token()


async def test_async_init_refreshes_expired_token(provider: SoundcloudMusicProvider) -> None:
    """An access token that expired while MA was down is refreshed before first use."""
    expired = f"OAuth {_jwt(exp=NOW - 60)}"
    renewed = f"OAuth {_jwt(exp=NOW + 3600)}"
    _setup_values(
        provider, client_id="test-client-id", authorization=expired, refresh_token="refresh"
    )
    refresh = AsyncMock(return_value=renewed)

    with (
        patch(
            "music_assistant.providers.soundcloud.SoundcloudAsyncAPI",
            return_value=_api_with_token(expired),
        ),
        patch("music_assistant.providers.soundcloud.time.time", return_value=NOW),
        patch.object(provider, "_refresh_access_token", refresh),
        patch.object(provider, "_schedule_token_refresh") as schedule,
    ):
        await provider.handle_async_init()

    refresh.assert_awaited_once()
    schedule.assert_called_once_with(renewed)


async def test_async_init_keeps_valid_token(provider: SoundcloudMusicProvider) -> None:
    """A token that is still valid is used as is and refreshed just before it expires."""
    valid = f"OAuth {_jwt(exp=NOW + 3600)}"
    _setup_values(
        provider, client_id="test-client-id", authorization=valid, refresh_token="refresh"
    )
    refresh = AsyncMock()

    with (
        patch(
            "music_assistant.providers.soundcloud.SoundcloudAsyncAPI",
            return_value=_api_with_token(valid),
        ),
        patch("music_assistant.providers.soundcloud.time.time", return_value=NOW),
        patch.object(provider, "_refresh_access_token", refresh),
        patch.object(provider.mass, "call_later") as call_later,
    ):
        await provider.handle_async_init()

    refresh.assert_not_awaited()
    assert call_later.call_args.args[0] == 3600 - TOKEN_REFRESH_BUFFER


async def test_legacy_token_is_never_refreshed(provider: SoundcloudMusicProvider) -> None:
    """An opaque token from the old login flow does not expire, so nothing is scheduled."""
    _setup_values(provider, refresh_token="refresh")

    with patch.object(provider.mass, "call_later") as call_later:
        provider._schedule_token_refresh("OAuth 2-123456-789-AbCdEf")

    call_later.assert_not_called()


async def test_jwt_without_refresh_token_is_not_scheduled(
    provider: SoundcloudMusicProvider,
) -> None:
    """Without a refresh token there is nothing to refresh with."""
    _setup_values(provider)

    with patch.object(provider.mass, "call_later") as call_later:
        provider._schedule_token_refresh(f"OAuth {_jwt(exp=NOW + 3600)}")

    call_later.assert_not_called()


async def test_scheduled_refresh_reschedules(provider: SoundcloudMusicProvider) -> None:
    """A successful scheduled refresh plans the next one with the new token."""
    with (
        patch.object(provider, "_refresh_access_token", AsyncMock(return_value="OAuth new")),
        patch.object(provider, "_schedule_token_refresh") as schedule,
    ):
        await provider._scheduled_token_refresh()

    schedule.assert_called_once_with("OAuth new")


async def test_scheduled_refresh_retries_network_error(
    provider: SoundcloudMusicProvider,
) -> None:
    """A network hiccup retries soon instead of unloading the provider."""
    with (
        patch.object(provider, "_refresh_access_token", AsyncMock(side_effect=ClientError())),
        patch.object(provider, "unload_with_error") as unload,
        patch.object(provider.mass, "call_later") as call_later,
    ):
        await provider._scheduled_token_refresh()

    unload.assert_not_called()
    call_later.assert_called_once()


async def test_scheduled_refresh_unloads_on_login_failure(
    provider: SoundcloudMusicProvider,
) -> None:
    """A refresh token Soundcloud rejects unloads the provider with the error."""
    error = LoginFailed("revoked")

    with (
        patch.object(provider, "_refresh_access_token", AsyncMock(side_effect=error)),
        patch.object(provider, "unload_with_error") as unload,
    ):
        await provider._scheduled_token_refresh()

    unload.assert_called_once_with(error)
