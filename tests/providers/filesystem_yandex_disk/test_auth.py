"""Tests for the local Yandex OAuth Device Flow and token refresh."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Self, cast

import aiohttp
import pytest
from music_assistant_models.errors import LoginFailed, ProviderUnavailableError

from music_assistant.providers.filesystem_yandex_disk.auth import (
    DeviceCodeDenied,
    DeviceCodeExpired,
    DevicePollState,
    MAYandexDiskAuth,
    OAuthProtocolError,
    OAuthTokens,
    OAuthTransportError,
    poll_device_token,
    request_device_code,
)
from music_assistant.providers.filesystem_yandex_disk.constants import (
    OAUTH_DEVICE_CODE_URL,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
)

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class _FakeResponse:
    """Minimal aiohttp response stand-in."""

    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def json(self, *, content_type: None = None) -> object:
        assert content_type is None
        return self._payload


class _FakeSession:
    """Queue responses and record OAuth form posts."""

    def __init__(self, *responses: _FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.posts: list[tuple[str, dict[str, str]]] = []

    def post(self, url: str, *, data: dict[str, str]) -> _FakeResponse:
        self.posts.append((url, data))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _MassStub:
    def __init__(self, session: _FakeSession) -> None:
        self.http_session = session


def _mass(session: _FakeSession) -> MusicAssistant:
    return cast("MusicAssistant", _MassStub(session))


def _device_payload() -> dict[str, object]:
    return {
        "device_code": "device-secret",
        "user_code": "ABCD-1234",
        "verification_url": "https://ya.ru/device",
        "expires_in": 300,
        "interval": 5,
    }


def _token_payload(
    access_token: str | None = None, refresh_token: str | None = None
) -> dict[str, object]:
    access_token = access_token or "access-token"
    refresh_token = refresh_token or "refresh-token"
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 3600,
    }


@pytest.mark.asyncio
async def test_request_device_code_uses_user_application_and_read_only_scope() -> None:
    """The code request uses the user's app and Disk read-only scope."""
    session = _FakeSession(_FakeResponse(200, _device_payload()))

    grant = await request_device_code(cast("Any", session), "client-id")

    assert grant.user_code == "ABCD-1234"
    assert grant.verification_url == "https://ya.ru/device"
    assert grant.expires_in == 300
    assert grant.interval == 5
    url, data = session.posts[0]
    assert url == OAUTH_DEVICE_CODE_URL
    assert data["client_id"] == "client-id"
    assert data["scope"] == OAUTH_SCOPE
    assert data["device_name"] == "Music Assistant - Yandex Disk"
    assert data["device_id"].isalnum()


@pytest.mark.asyncio
async def test_request_device_code_rejects_malformed_response_without_leaking_payload() -> None:
    """A malformed response is reported without embedding its body."""
    session = _FakeSession(_FakeResponse(200, {"device_code": "do-not-leak"}))

    with pytest.raises(OAuthProtocolError) as err:
        await request_device_code(cast("Any", session), "client-id")

    assert "do-not-leak" not in str(err.value)


@pytest.mark.asyncio
async def test_request_device_code_maps_server_and_network_failures() -> None:
    """Server and transport failures remain retryable setup errors."""
    server = _FakeSession(_FakeResponse(503, {"error": "server_error"}))
    network = _FakeSession(aiohttp.ClientError("offline"))

    with pytest.raises(OAuthTransportError):
        await request_device_code(cast("Any", server), "client-id")
    with pytest.raises(OAuthTransportError):
        await request_device_code(cast("Any", network), "client-id")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("authorization_pending", DevicePollState.PENDING),
        ("slow_down", DevicePollState.SLOW_DOWN),
    ],
)
async def test_poll_device_token_returns_non_terminal_state(
    error: str, expected: DevicePollState
) -> None:
    """Pending and slow-down replies are represented as polling states."""
    session = _FakeSession(_FakeResponse(400, {"error": error}))
    grant_session = _FakeSession(_FakeResponse(200, _device_payload()))
    grant = await request_device_code(cast("Any", grant_session), "client-id")

    result = await poll_device_token(cast("Any", session), grant, "client-id", "secret")

    assert result is expected


@pytest.mark.asyncio
async def test_poll_device_token_returns_tokens_and_sends_client_secret() -> None:
    """Confirmed polling returns both tokens and authenticates the app."""
    session = _FakeSession(_FakeResponse(200, _token_payload()))
    grant_session = _FakeSession(_FakeResponse(200, _device_payload()))
    grant = await request_device_code(cast("Any", grant_session), "client-id")

    result = await poll_device_token(cast("Any", session), grant, "client-id", "secret")

    assert isinstance(result, OAuthTokens)
    assert result.access_token == "access-token"
    assert result.refresh_token == "refresh-token"
    url, data = session.posts[0]
    assert url == OAUTH_TOKEN_URL
    assert data == {
        "grant_type": "device_code",
        "code": "device-secret",
        "client_id": "client-id",
        "client_secret": "secret",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "exception"),
    [
        ("expired_token", DeviceCodeExpired),
        ("access_denied", DeviceCodeDenied),
    ],
)
async def test_poll_device_token_maps_terminal_state(
    error: str, exception: type[Exception]
) -> None:
    """Expired and denied device grants are distinguishable."""
    session = _FakeSession(_FakeResponse(400, {"error": error}))
    grant_session = _FakeSession(_FakeResponse(200, _device_payload()))
    grant = await request_device_code(cast("Any", grant_session), "client-id")

    with pytest.raises(exception):
        await poll_device_token(cast("Any", session), grant, "client-id", "secret")


@pytest.mark.asyncio
async def test_auth_refresh_caches_access_token_and_persists_rotation() -> None:
    """Refresh is cached and a rotated refresh token is persisted immediately."""
    session = _FakeSession(_FakeResponse(200, _token_payload(refresh_token="rotated")))
    persisted: list[str] = []
    helper = MAYandexDiskAuth(
        _mass(session), "client-id", "secret", "initial-refresh", persisted.append
    )

    assert await helper.async_get_access_token() == "access-token"
    assert await helper.async_get_access_token() == "access-token"
    assert persisted == ["rotated"]
    assert len(session.posts) == 1


@pytest.mark.asyncio
async def test_auth_concurrent_callers_share_one_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent cache misses redeem the refresh token only once."""
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    refresh_calls = 0

    async def refresh_once(*_args: object) -> OAuthTokens:
        nonlocal refresh_calls
        refresh_calls += 1
        refresh_started.set()
        await release_refresh.wait()
        return OAuthTokens("shared-access", "refresh", 3600)

    monkeypatch.setattr(
        "music_assistant.providers.filesystem_yandex_disk.auth.refresh_oauth_tokens",
        refresh_once,
    )
    helper = MAYandexDiskAuth(_mass(_FakeSession()), "client-id", "secret", "refresh")

    first = asyncio.create_task(helper.async_get_access_token())
    await refresh_started.wait()
    second = asyncio.create_task(helper.async_get_access_token())
    await asyncio.sleep(0)
    release_refresh.set()

    assert tuple(await asyncio.gather(first, second)) == ("shared-access", "shared-access")
    assert refresh_calls == 1


@pytest.mark.asyncio
async def test_auth_refresh_invalid_grant_is_login_failed() -> None:
    """Only explicit invalid_grant is treated as dead credentials."""
    session = _FakeSession(_FakeResponse(400, {"error": "invalid_grant"}))
    helper = MAYandexDiskAuth(_mass(session), "client-id", "secret", "refresh")

    with pytest.raises(LoginFailed):
        await helper.async_get_access_token()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(503, {"error": "server_error"}),
        _FakeResponse(400, {"error": "temporarily_unavailable"}),
        aiohttp.ClientError("offline"),
    ],
)
async def test_auth_refresh_unknown_failure_is_provider_unavailable(
    response: _FakeResponse | Exception,
) -> None:
    """Unproven credential failures remain transient."""
    session = _FakeSession(response)
    helper = MAYandexDiskAuth(_mass(session), "client-id", "secret", "refresh")

    with pytest.raises(ProviderUnavailableError):
        await helper.async_get_access_token()


def test_oauth_models_redact_secrets_from_repr() -> None:
    """Debug representations cannot expose tokens or device secrets."""
    tokens = OAuthTokens("access-secret", "refresh-secret", 3600)

    assert "access-secret" not in repr(tokens)
    assert "refresh-secret" not in repr(tokens)
