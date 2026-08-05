"""Unit tests for the Apple Music API client error handling and pagination."""

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientPayloadError, ClientResponseError, ServerDisconnectedError
from aiohttp.client_reqrep import RequestInfo
from multidict import CIMultiDict, CIMultiDictProxy
from music_assistant_models.errors import (
    LoginFailed,
    ResourceTemporarilyUnavailable,
    RetriesExhausted,
)
from yarl import URL

from music_assistant.providers.apple_music.api_client import (
    _LIBRARY_PAGE_SIZE,
    _PAGE_TRUNCATION_RETRIES,
    AppleMusicAPIClient,
    _retry_transient_transport_errors,
)

# Target for patching the backoff/throttle sleep so retries run instantly.
_SLEEP_TARGET = "music_assistant.helpers.throttle_retry.asyncio.sleep"


class _FakeRequestCtx:
    """Minimal async context manager mimicking aiohttp's request context."""

    def __init__(self, response: MagicMock) -> None:
        self._response = response

    async def __aenter__(self) -> MagicMock:
        return self._response

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _make_response(
    status: int = 200,
    json_data: Any = None,
    json_exc: BaseException | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.headers = CIMultiDict()
    response.content_length = 0
    response.raise_for_status = MagicMock()
    if json_exc is not None:
        response.json = AsyncMock(side_effect=json_exc)
    else:
        response.json = AsyncMock(return_value=json_data)
    return response


def _make_client() -> tuple[AppleMusicAPIClient, MagicMock]:
    """Return an API client together with its mock provider (for session stubbing)."""
    provider = MagicMock()
    provider.logger = MagicMock()
    provider._music_app_token = "app-token"
    provider._music_user_token = "user-token"
    return AppleMusicAPIClient(provider), provider


def _client_response_error(status: int) -> ClientResponseError:
    request_info = RequestInfo(
        url=URL("https://api.music.apple.com/v1/me/library/songs"),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("https://api.music.apple.com/v1/me/library/songs"),
    )
    return ClientResponseError(
        request_info=request_info, history=(), status=status, message="error", headers=None
    )


# ---------------------------------------------------------------------------
# P1: transient transport errors map to the retryable error type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transport_error",
    [
        ClientPayloadError("Not enough data to satisfy content length header."),
        ServerDisconnectedError("Server disconnected"),
        TimeoutError("Read timed out"),
    ],
)
@pytest.mark.asyncio
async def test_transient_transport_errors_mapped_to_retryable(
    transport_error: BaseException,
) -> None:
    """Transport-level errors are converted to ResourceTemporarilyUnavailable."""

    async def _raise(_self: object) -> None:
        raise transport_error

    wrapped = _retry_transient_transport_errors(_raise)
    with pytest.raises(ResourceTemporarilyUnavailable):
        await wrapped(object())


@pytest.mark.asyncio
async def test_client_response_error_not_mapped() -> None:
    """HTTP status errors (raise_for_status) must not be silently retried."""

    async def _raise(_self: object) -> None:
        raise _client_response_error(403)

    wrapped = _retry_transient_transport_errors(_raise)
    with pytest.raises(ClientResponseError):
        await wrapped(object())


@pytest.mark.asyncio
async def test_decorator_passes_through_success() -> None:
    """The decorator returns the wrapped result unchanged on success."""

    async def _ok(_self: object, value: int) -> int:
        return value

    wrapped: Callable[[object, int], Awaitable[int]] = _retry_transient_transport_errors(_ok)
    assert await wrapped(object(), 42) == 42


@pytest.mark.asyncio
async def test_get_data_recovers_after_transient_payload_error() -> None:
    """A truncated body on the first attempt is retried and then succeeds."""
    client, provider = _make_client()
    payload = {"data": [{"id": "1"}]}
    provider.mass.http_session.get = MagicMock(
        side_effect=[
            _FakeRequestCtx(_make_response(status=200, json_exc=ClientPayloadError("truncated"))),
            _FakeRequestCtx(_make_response(status=200, json_data=payload)),
        ]
    )
    with patch(_SLEEP_TARGET, new=AsyncMock()):
        result = await client.get_data("me/library/songs", limit=50, offset=0)
    assert result == payload
    assert provider.mass.http_session.get.call_count == 2


# ---------------------------------------------------------------------------
# P2: HTTP 500 is retryable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_data_500_recovers_on_retry() -> None:
    """A transient 500 is retried and then succeeds."""
    client, provider = _make_client()
    payload = {"data": [{"id": "1"}]}
    provider.mass.http_session.get = MagicMock(
        side_effect=[
            _FakeRequestCtx(_make_response(status=500)),
            _FakeRequestCtx(_make_response(status=200, json_data=payload)),
        ]
    )
    with patch(_SLEEP_TARGET, new=AsyncMock()):
        result = await client.get_data("me/library/songs", limit=50, offset=0)
    assert result == payload


@pytest.mark.asyncio
async def test_get_data_persistent_500_exhausts_retries() -> None:
    """A persistent 500 retries retry_attempts times then raises RetriesExhausted."""
    client, provider = _make_client()
    provider.mass.http_session.get = MagicMock(
        return_value=_FakeRequestCtx(_make_response(status=500))
    )
    with patch(_SLEEP_TARGET, new=AsyncMock()), pytest.raises(RetriesExhausted):
        await client.get_data("me/library/songs", limit=50, offset=0)
    assert provider.mass.http_session.get.call_count == client.throttler.retry_attempts


# ---------------------------------------------------------------------------
# P3: pagination distinguishes a clean/empty end from a mid-list truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_items_assembles_pages() -> None:
    """Pages are concatenated and pagination stops when no `next` is returned."""
    client, _ = _make_client()
    client.get_data = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"data": [{"id": "a"}, {"id": "b"}], "next": "/v1/me/library/songs?offset=50"},
            {"data": [{"id": "c"}]},
        ]
    )
    items = await client.get_all_items("me/library/songs")
    assert [item["id"] for item in items] == ["a", "b", "c"]
    assert client.get_data.call_args_list[0].kwargs["offset"] == 0
    assert client.get_data.call_args_list[1].kwargs["offset"] == _LIBRARY_PAGE_SIZE


@pytest.mark.asyncio
async def test_iter_all_items_fetches_pages_lazily() -> None:
    """iter_all_items yields incrementally and only fetches the next page when needed."""
    client, _ = _make_client()
    client.get_data = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"data": [{"id": "a"}, {"id": "b"}], "next": "/v1/me/library/songs?offset=50"},
            {"data": [{"id": "c"}]},
        ]
    )
    gen = client.iter_all_items("me/library/songs")
    first = await anext(gen)
    assert first["id"] == "a"
    # The second page must not have been fetched just to yield the first item.
    assert client.get_data.call_count == 1
    rest = [item async for item in gen]
    assert [item["id"] for item in rest] == ["b", "c"]
    assert client.get_data.call_count == 2


@pytest.mark.asyncio
async def test_get_all_items_empty_first_page_returns_empty() -> None:
    """An empty collection (empty data, no `next`) yields an empty list."""
    client, _ = _make_client()
    client.get_data = AsyncMock(return_value={"data": []})  # type: ignore[method-assign]
    assert await client.get_all_items("me/library/songs") == []


@pytest.mark.asyncio
async def test_get_all_items_first_page_404_returns_empty() -> None:
    """A 404 on the very first page (no prior page) is treated as empty, not an error."""
    client, _ = _make_client()
    client.get_data = AsyncMock(return_value={})  # type: ignore[method-assign]
    assert await client.get_all_items("me/library/songs") == []


@pytest.mark.asyncio
async def test_get_all_items_midlist_truncation_recovers() -> None:
    """A transient mid-list 404 is retried in place and the listing completes."""
    client, _ = _make_client()
    client.get_data = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"data": [{"id": "a"}], "next": "/v1/me/library/songs?offset=50"},
            {},  # transient truncation on the second page
            {"data": [{"id": "b"}]},  # in-place retry succeeds
        ]
    )
    items = await client.get_all_items("me/library/songs")
    assert [item["id"] for item in items] == ["a", "b"]


@pytest.mark.asyncio
async def test_get_all_items_persistent_truncation_raises() -> None:
    """A 404 that persists across in-place retries surfaces as a loud failure."""
    client, _ = _make_client()
    client.get_data = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"data": [{"id": "a"}], "next": "/v1/me/library/songs?offset=50"},
            *([{}] * (_PAGE_TRUNCATION_RETRIES + 1)),
        ]
    )
    with pytest.raises(ResourceTemporarilyUnavailable):
        await client.get_all_items("me/library/songs")


# ---------------------------------------------------------------------------
# P4: a rejected music user token surfaces as an auth error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_get_data_auth_error_raises_login_failed(status: int) -> None:
    """A revoked/expired user token surfaces as LoginFailed, not a bare HTTP error."""
    client, provider = _make_client()
    provider.mass.http_session.get = MagicMock(
        return_value=_FakeRequestCtx(_make_response(status=status))
    )
    with patch(_SLEEP_TARGET, new=AsyncMock()), pytest.raises(LoginFailed):
        await client.get_data("me/storefront")
    # the token will not become valid on its own, so this must not be retried
    assert provider.mass.http_session.get.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_write_requests_auth_error_raises_login_failed(status: int) -> None:
    """Library mutations report a rejected user token as LoginFailed too."""
    client, provider = _make_client()
    for method in ("put", "post", "delete"):
        setattr(
            provider.mass.http_session,
            method,
            MagicMock(return_value=_FakeRequestCtx(_make_response(status=status))),
        )
    with patch(_SLEEP_TARGET, new=AsyncMock()):
        with pytest.raises(LoginFailed):
            await client.put_data("me/ratings/library-playlists/p.1")
        with pytest.raises(LoginFailed):
            await client.post_data("me/library")
        with pytest.raises(LoginFailed):
            await client.delete_data("me/library/playlists/p.1")


# ---------------------------------------------------------------------------
# P5: a momentary 429 recovers quickly instead of stalling playback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_data_429_retries_within_a_second() -> None:
    """A 429 is retried after ~1s, so a throttled boundary fetch does not stall playback."""
    client, provider = _make_client()
    payload = {"data": [{"id": "1"}]}
    provider.mass.http_session.get = MagicMock(
        side_effect=[
            _FakeRequestCtx(_make_response(status=429)),
            _FakeRequestCtx(_make_response(status=200, json_data=payload)),
        ]
    )
    sleep_mock = AsyncMock()
    with patch(_SLEEP_TARGET, new=sleep_mock):
        result = await client.get_data("me/library/songs", limit=50, offset=0)
    assert result == payload
    sleeps = [call.args[0] for call in sleep_mock.await_args_list]
    assert sleeps, "expected the 429 to be retried after a backoff"
    # jitter adds at most 10% on top of the initial backoff
    assert max(sleeps) <= 1.1


@pytest.mark.asyncio
async def test_get_data_sustained_429_is_ridden_out_for_minutes() -> None:
    """Throttling that outlasts the first retries is waited out, not failed within seconds."""
    client, provider = _make_client()
    provider.mass.http_session.get = MagicMock(
        return_value=_FakeRequestCtx(_make_response(status=429))
    )
    sleep_mock = AsyncMock()
    with patch(_SLEEP_TARGET, new=sleep_mock), pytest.raises(RetriesExhausted):
        await client.get_data("me/library/songs", limit=50, offset=0)
    sleeps = [call.args[0] for call in sleep_mock.await_args_list]
    assert sum(sleeps) > 100
