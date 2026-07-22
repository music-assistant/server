"""Tests for the Yandex Disk OAuth code flow and token refresh."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError

from music_assistant.providers.filesystem_yandex_disk import auth
from music_assistant.providers.filesystem_yandex_disk.constants import (
    OAUTH_AUTHORIZE_URL,
    VERIFICATION_CODE_REDIRECT,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class _FakeResp:
    """Async-context-manager stand-in for an aiohttp response."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def text(self) -> str:
        return str(self._payload)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientError(f"HTTP {self.status}")


class _FakeSession:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.posts: list[tuple[str, Any]] = []

    def post(self, url: str, data: Any = None) -> _FakeResp:
        self.posts.append((url, data))
        return self._resp


class _MassStub:
    def __init__(self, resp: _FakeResp | None = None) -> None:
        self.http_session = _FakeSession(resp if resp is not None else _FakeResp(200, {}))


def _mass(resp: _FakeResp | None = None) -> MusicAssistant:
    """Return a stand-in MusicAssistant wrapping a fake http session."""
    return cast("MusicAssistant", _MassStub(resp))


def test_manual_authorize_url_contains_client_scope_and_redirect() -> None:
    """The manual authorize URL carries the client, scope and OOB redirect."""
    url = auth.manual_authorize_url("cid123")
    assert url.startswith(OAUTH_AUTHORIZE_URL)
    params = parse_qs(urlparse(url).query)
    assert params["client_id"] == ["cid123"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["cloud_api:disk.read"]
    assert params["redirect_uri"] == [VERIFICATION_CODE_REDIRECT]


@pytest.mark.asyncio
async def test_exchange_manual_code_empty_raises() -> None:
    """An empty confirmation code is a terminal auth failure."""
    with pytest.raises(LoginFailed):
        await auth.exchange_manual_code(_mass(), "", "cid", "secret")


@pytest.mark.asyncio
async def test_exchange_manual_code_returns_refresh_token() -> None:
    """A successful code exchange returns the refresh token."""
    mass = _mass(_FakeResp(200, {"access_token": "at", "refresh_token": "rt"}))
    rt = await auth.exchange_manual_code(mass, "the-code", "cid", "secret")
    assert rt == "rt"


@pytest.mark.asyncio
async def test_exchange_missing_refresh_token_raises() -> None:
    """A token response without a refresh token is an error."""
    mass = _mass(_FakeResp(200, {"access_token": "at"}))
    with pytest.raises(LoginFailed):
        await auth.exchange_manual_code(mass, "the-code", "cid", "secret")


@pytest.mark.asyncio
async def test_auth_refresh_returns_and_caches_access_token() -> None:
    """MAYandexDiskAuth exchanges the refresh token and caches the access token."""
    stub = _MassStub(_FakeResp(200, {"access_token": "at1", "expires_in": 3600}))
    helper = auth.MAYandexDiskAuth(cast("MusicAssistant", stub), "cid", "secret", "rt")
    assert await helper.async_get_access_token() == "at1"
    assert await helper.async_get_access_token() == "at1"  # cached, no 2nd POST
    assert len(stub.http_session.posts) == 1


@pytest.mark.asyncio
async def test_auth_refresh_rejected_raises_login_failed() -> None:
    """A rejected refresh token surfaces as LoginFailed."""
    helper = auth.MAYandexDiskAuth(
        _mass(_FakeResp(400, {"error": "invalid_grant"})), "cid", "secret", "rt"
    )
    with pytest.raises(LoginFailed):
        await helper.async_get_access_token()


@pytest.mark.asyncio
async def test_auth_refresh_server_error_is_transient() -> None:
    """A 5xx during refresh is transient, not a credential failure."""
    helper = auth.MAYandexDiskAuth(_mass(_FakeResp(503, {})), "cid", "secret", "rt")
    with pytest.raises(ProviderUnavailableError):
        await helper.async_get_access_token()


@pytest.mark.asyncio
async def test_auth_no_refresh_token_raises() -> None:
    """Without a stored refresh token, access-token retrieval fails clearly."""
    helper = auth.MAYandexDiskAuth(_mass(), "cid", "secret", "")
    with pytest.raises(LoginFailed):
        await helper.async_get_access_token()
