"""Tests for the yadisk-backed API client mapping and token handling."""

from __future__ import annotations

import time
from typing import Any, cast

import aiohttp
import pytest
import yarl
from multidict import CIMultiDict, CIMultiDictProxy
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
)

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


_SIGNED = "https://downloader.disk.yandex.ru/disk/abc?sign=SECRET-SIGNATURE"


class _FailingGet:
    """``http_session.get`` stand-in that raises the given error."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __call__(self, _url: str, **_kwargs: Any) -> _FailingGet:
        return self

    def __await__(self) -> Any:
        return self._raise().__await__()

    async def _raise(self) -> None:
        raise self.error

    async def __aenter__(self) -> None:
        raise self.error

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _api_with_get(error: BaseException) -> YandexDiskApi:
    class _Session:
        get = _FailingGet(error)

    class _Mass:
        http_session = _Session()

    api = YandexDiskApi.__new__(YandexDiskApi)
    api.mass = cast("Any", _Mass())

    async def _link(_path: str) -> str:
        return _SIGNED

    cast("Any", api)._download_link = _link
    return api


def _signed_url_errors() -> list[BaseException]:
    request_info = aiohttp.RequestInfo(
        url=yarl.URL(_SIGNED),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=yarl.URL(_SIGNED),
    )
    return [
        aiohttp.ClientResponseError(request_info, (), status=403, message="Forbidden"),
        aiohttp.TooManyRedirects(request_info, ()),
    ]


async def _download(api: YandexDiskApi, method: str) -> object:
    if method == "bytes":
        return await api.download_bytes("disk:/a.nfo")
    return await api.download_response("disk:/a.flac", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["bytes", "stream"])
async def test_download_timeout_is_provider_unavailable(method: str) -> None:
    """A total timeout surfaces as the typed provider error the cloud base expects."""
    api = _api_with_get(TimeoutError())
    with pytest.raises(ProviderUnavailableError):
        await _download(api, method)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["bytes", "stream"])
@pytest.mark.parametrize("error", _signed_url_errors(), ids=["http-error", "redirects"])
async def test_download_errors_never_carry_the_signed_link(
    method: str, error: BaseException
) -> None:
    """Neither the message nor the exception chain exposes the pre-signed URL."""
    api = _api_with_get(error)
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await _download(api, method)

    err = exc_info.value
    assert "SECRET-SIGNATURE" not in str(err)
    assert "downloader.disk.yandex.ru" not in str(err)
    assert err.__cause__ is None
    assert err.__suppress_context__ is True


class _JsonResponse:
    """aiohttp response stand-in carrying a Yandex Disk API JSON body."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        self._body = body

    async def json(self, **_kwargs: Any) -> dict[str, Any]:
        return self._body

    async def release(self) -> None:
        return None


def _item(name: str, kind: str = "file", **extra: Any) -> dict[str, Any]:
    return {"name": name, "path": f"disk:/Music/{name}", "type": kind, **extra}


class _DiskApiSession:
    """Serves paginated Yandex Disk listings (limit 2) for ``disk:/Music``."""

    ITEMS = (
        _item("Album", "dir", modified="2026-10-01T10:00:00+00:00"),
        _item("a.flac", md5="md5a", size=10, modified="2026-10-02T10:00:00+00:00"),
        _item("b.mp3", size=5, modified="2026-10-03T10:00:00+00:00"),
    )

    def __init__(self) -> None:
        self.offsets: list[int] = []

    async def request(self, _method: str, _url: str, **kwargs: Any) -> _JsonResponse:
        params = kwargs.get("params") or {}
        if params.get("path") != "disk:/Music":
            return _JsonResponse(
                404,
                {
                    "error": "DiskNotFoundError",
                    "message": "Resource not found.",
                    "description": "Resource not found.",
                },
            )
        offset = int(params.get("offset", 0))
        self.offsets.append(offset)
        page = list(self.ITEMS[offset : offset + 2])
        return _JsonResponse(
            200,
            {
                "type": "dir",
                "name": "Music",
                "path": "disk:/Music",
                "_embedded": {
                    "items": page,
                    "limit": 2,
                    "offset": offset,
                    "total": len(self.ITEMS),
                    "path": "disk:/Music",
                    "sort": "",
                },
            },
        )


def _api_over(session: _DiskApiSession) -> YandexDiskApi:
    class _Mass:
        http_session = session

    return YandexDiskApi(cast("Any", _Mass()), cast("Any", _AuthStub()))


@pytest.mark.asyncio
async def test_list_children_follows_pagination_through_yadisk() -> None:
    """Real yadisk listing over Disk API JSON pages maps every child."""
    session = _DiskApiSession()
    items = await _api_over(session).list_children("disk:/Music")

    assert session.offsets == [0, 2]
    # yadisk parses ``modified`` into a datetime, so the token is its str() form
    assert items == [
        ("disk:/Music/Album", "Album", True, "", None, "2026-10-01 10:00:00+00:00"),
        ("disk:/Music/a.flac", "a.flac", False, "md5a", 10, "2026-10-02 10:00:00+00:00"),
        (
            "disk:/Music/b.mp3",
            "b.mp3",
            False,
            "2026-10-03 10:00:00+00:00",
            5,
            "2026-10-03 10:00:00+00:00",
        ),
    ]


@pytest.mark.asyncio
async def test_list_children_missing_folder_is_media_not_found() -> None:
    """A Disk API 404 is translated to MediaNotFoundError."""
    with pytest.raises(MediaNotFoundError):
        await _api_over(_DiskApiSession()).list_children("disk:/Missing")
