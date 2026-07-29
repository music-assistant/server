"""Tests for Yandex Disk access-token caching and shared OAuth refresh."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import OAuthTokens, SecretStr

from music_assistant.providers.filesystem_yandex_disk import auth

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class _MassStub:
    def __init__(self) -> None:
        self.http_session = object()


def _mass() -> MusicAssistant:
    return cast("MusicAssistant", _MassStub())


def _tokens(access: str = "access-token", refresh: str = "rotated-refresh") -> OAuthTokens:
    return OAuthTokens(
        access_token=SecretStr(access),
        refresh_token=SecretStr(refresh),
        expires_in=3600,
    )


@pytest.mark.asyncio
async def test_auth_refresh_uses_shared_helper_and_caches_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first request rotates tokens; the second uses the access-token cache."""
    calls: list[dict[str, object]] = []

    async def refresh_oauth_tokens(**kwargs: object) -> OAuthTokens:
        calls.append(kwargs)
        return _tokens()

    monkeypatch.setattr(auth, "refresh_oauth_tokens", refresh_oauth_tokens)
    mass = _mass()
    helper = auth.MAYandexDiskAuth(mass, "cid", "secret", "initial-refresh")

    assert await helper.async_get_access_token() == "access-token"
    assert await helper.async_get_access_token() == "access-token"
    assert calls == [
        {
            "client_id": "cid",
            "client_secret": "secret",
            "refresh_token": "initial-refresh",
            "scope": "cloud_api:disk.read",
            "session": mass.http_session,
        }
    ]


@pytest.mark.asyncio
async def test_auth_keeps_rotated_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A later refresh uses the most recently rotated refresh token."""
    seen: list[str] = []

    async def refresh_oauth_tokens(**kwargs: object) -> OAuthTokens:
        seen.append(str(kwargs["refresh_token"]))
        return _tokens(access=f"access-{len(seen)}", refresh=f"refresh-{len(seen)}")

    monkeypatch.setattr(auth, "refresh_oauth_tokens", refresh_oauth_tokens)
    persisted: list[str] = []
    helper = auth.MAYandexDiskAuth(_mass(), "cid", "secret", "refresh-0", persisted.append)

    assert await helper._refresh() == "access-1"
    assert await helper._refresh() == "access-2"
    assert seen == ["refresh-0", "refresh-1"]
    assert persisted == ["refresh-1", "refresh-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [LoginFailed("rejected"), ResourceTemporarilyUnavailable("temporary")],
)
async def test_auth_refresh_propagates_mapped_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """The shared library's terminal/transient distinction is preserved."""

    async def refresh_oauth_tokens(**_kwargs: object) -> OAuthTokens:
        raise error

    monkeypatch.setattr(auth, "refresh_oauth_tokens", refresh_oauth_tokens)
    helper = auth.MAYandexDiskAuth(_mass(), "cid", "secret", "refresh")
    with pytest.raises(type(error)):
        await helper.async_get_access_token()


@pytest.mark.asyncio
async def test_auth_no_refresh_token_raises() -> None:
    """Without a stored refresh token, access-token retrieval fails clearly."""
    helper = auth.MAYandexDiskAuth(_mass(), "cid", "secret", "")
    with pytest.raises(LoginFailed):
        await helper.async_get_access_token()
