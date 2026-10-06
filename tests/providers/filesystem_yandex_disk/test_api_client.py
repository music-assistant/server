"""Tests for the yadisk-backed API client mapping and token handling."""

from __future__ import annotations

import time
from typing import Any, cast

import aiohttp
import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.filesystem_yandex_disk.api_client import (
    YandexDiskApi,
    _SharedAIOHTTPSession,
    _to_raw_item,
)


class _Resource:
    """Minimal stand-in for a yadisk resource object."""

    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def test_to_raw_item_file() -> None:
    """A file resource maps to a RawItem with checksum, size, and metadata token."""
    res = _Resource(
        path="disk:/Music/a.flac",
        name="a.flac",
        type="file",
        md5="abc",
        size=123,
        modified="2026-08-28T07:00:00Z",
    )
    assert _to_raw_item(res) == (
        "disk:/Music/a.flac",
        "a.flac",
        False,
        "abc",
        123,
        "2026-08-28T07:00:00Z",
    )


def test_to_raw_item_dir_has_empty_checksum_and_no_size() -> None:
    """A directory has no size and an empty checksum."""
    res = _Resource(path="disk:/Music", name="Music", type="dir", md5=None, size=None)
    assert _to_raw_item(res) == ("disk:/Music", "Music", True, "", None, None)


def test_to_raw_item_file_without_md5_uses_modified_as_checksum() -> None:
    """A file lacking md5 falls back to its modification time so replacements rescan."""
    res = _Resource(
        path="disk:/x.mp3",
        name="x.mp3",
        type="file",
        md5=None,
        size=1,
        modified="2026-10-05T18:00:00Z",
    )
    _id, _name, is_dir, checksum, size, metadata_token = _to_raw_item(res)
    assert is_dir is False
    assert checksum == "2026-10-05T18:00:00Z"
    assert size == 1
    assert metadata_token == "2026-10-05T18:00:00Z"


def test_to_raw_item_file_without_md5_or_modified_has_empty_checksum() -> None:
    """A file with neither md5 nor modified time never gets a constant sentinel."""
    res = _Resource(path="disk:/x.mp3", name="x.mp3", type="file", md5=None, size=1)
    _id, _name, _is_dir, checksum, _size, metadata_token = _to_raw_item(res)
    assert checksum == ""
    assert metadata_token is None


class _AuthStub:
    async def async_get_access_token(self) -> str:
        return "at"


@pytest.mark.asyncio
async def test_validate_refreshes_and_accepts_token() -> None:
    """validate() pulls a fresh access token and accepts a valid one."""
    api = YandexDiskApi.__new__(YandexDiskApi)
    api._auth = cast("Any", _AuthStub())

    class _Client:
        token = ""

        async def check_token(self) -> bool:
            return True

    api._client = cast("Any", _Client())
    await api.validate()
    assert api._client.token == "at"  # refreshed onto the yadisk client


@pytest.mark.asyncio
async def test_validate_rejected_token_raises() -> None:
    """A token the API rejects surfaces as LoginFailed."""
    api = YandexDiskApi.__new__(YandexDiskApi)
    api._auth = cast("Any", _AuthStub())

    class _Client:
        token = ""

        async def check_token(self) -> bool:
            return False

    api._client = cast("Any", _Client())
    with pytest.raises(LoginFailed):
        await api.validate()


@pytest.mark.asyncio
async def test_download_response_disables_total_timeout() -> None:
    """Streams are not cut off by the shared session's total timeout."""
    calls: list[dict[str, Any]] = []

    class _Session:
        async def get(self, url: str, **kwargs: Any) -> str:
            calls.append({"url": url, **kwargs})
            return "response"

    class _Mass:
        http_session = _Session()

    api = YandexDiskApi.__new__(YandexDiskApi)
    api.mass = cast("Any", _Mass())

    async def _link(_path: str) -> str:
        return "https://downloader.disk.yandex.ru/x"

    cast("Any", api)._download_link = _link
    result: object = await api.download_response("disk:/x.flac", {"Range": "bytes=0-"})

    assert result == "response"
    timeout = calls[0]["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout)
    assert timeout == aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=60)
    assert calls[0]["headers"] == {"Range": "bytes=0-"}


class _RawResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}


class _RecordingSession:
    """Stand-in for MA's shared aiohttp session that records request times."""

    def __init__(self, *responses: _RawResponse) -> None:
        self.responses = list(responses)
        self.sent_at: list[float] = []

    async def request(self, _method: str, _url: str, **_kwargs: Any) -> _RawResponse:
        self.sent_at.append(time.monotonic())
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_disk_api_requests_are_rate_limited() -> None:
    """Disk API requests share one throttler, so bursts are paced."""
    shared = _RecordingSession(*(_RawResponse(200) for _ in range(3)))
    session = _SharedAIOHTTPSession(cast("Any", shared), ThrottlerManager(rate_limit=1, period=0.2))

    for _ in range(3):
        await session.send_request("GET", "https://cloud-api.yandex.net/v1/disk")

    assert shared.sent_at[-1] - shared.sent_at[0] >= 0.35


@pytest.mark.asyncio
async def test_rate_limited_response_holds_back_later_requests() -> None:
    """A 429 with Retry-After arms a cooldown that later requests wait out."""
    shared = _RecordingSession(_RawResponse(429, {"Retry-After": "7"}))
    throttler = ThrottlerManager(rate_limit=10, period=1)
    session = _SharedAIOHTTPSession(cast("Any", shared), throttler)

    response = await session.send_request("GET", "https://cloud-api.yandex.net/v1/disk")

    assert response.status == 429
    assert 6 < throttler.cooldown_remaining <= 7


def test_api_client_throttles_its_disk_session() -> None:
    """The yadisk client is wired to a throttled session."""

    class _Mass:
        http_session = object()

    api = YandexDiskApi(cast("Any", _Mass()), cast("Any", object()))

    session = api._client.session
    assert isinstance(session, _SharedAIOHTTPSession)
    assert isinstance(session.throttler, ThrottlerManager)
