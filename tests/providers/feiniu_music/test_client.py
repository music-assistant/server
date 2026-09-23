"""Synthetic fixtures and loopback HTTP failures; no NAS or real credentials."""

import asyncio
import errno
import json
import socket
import traceback
from collections.abc import AsyncIterator
from typing import Any, Self
from unittest.mock import AsyncMock

import aiohttp
import pytest
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
    RateLimited,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)

from music_assistant.providers.feiniu_music.client import (
    AuthenticationError,
    FeiNiuClient,
    NetworkError,
    NotFoundError,
    PermissionDeniedError,
    ProtocolError,
    ProtocolProfile,
    RateLimitError,
    StreamRejectedError,
    classify_media,
    make_signature,
)


@pytest.mark.parametrize(
    ("client_error", "ma_error"),
    [
        (AuthenticationError, LoginFailed),
        (NotFoundError, MediaNotFoundError),
        (PermissionDeniedError, ProviderPermissionDenied),
        (StreamRejectedError, UnplayableMediaError),
        (ProtocolError, InvalidDataError),
        (NetworkError, ResourceTemporarilyUnavailable),
        (RateLimitError, RateLimited),
    ],
)
def test_client_errors_inherit_ma_types(client_error: Any, ma_error: Any) -> None:
    """Native errors have MA semantics without a provider conversion step."""
    error = client_error("synthetic")
    assert isinstance(error, ma_error)
    assert error.error_code == ma_error.error_code
    assert error.translation_key == ma_error.translation_key


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"code": 100004, "msg": "synthetic private path"}, StreamRejectedError),
        ({"code": 120001}, AuthenticationError),
        ({"code": 100005}, NotFoundError),
        ({"code": 987654, "msg": "SECRET"}, ProtocolError),
    ],
)
async def test_audio_business_errors_never_yield_decoder_bytes(
    payload: dict[str, Any], error: Any
) -> None:
    """HTTP 200 cannot turn a bounded JSON business failure into audio."""
    client = client_with(Response(json.dumps(payload).encode()))
    client._token = "synthetic-token"
    with pytest.raises(error) as raised:
        await anext(client.audio_stream("synthetic-id"))
    assert "private path" not in str(raised.value)
    assert "SECRET" not in str(raised.value)
    assert client._token == "synthetic-token"


async def test_validated_range_probe_rejects_business_json() -> None:
    """The StreamDetails preflight uses the same bounded validation as playback."""
    client = client_with(Response(b'{"code":100004}'))
    with pytest.raises(StreamRejectedError):
        await client.media_prefix("audio", "synthetic-id", limit=4096, validate=True)


async def test_error_json_is_bounded_and_unrelated_code_is_not_permission() -> None:
    """An oversized body stops at the prefix limit; 100004 remains generic elsewhere."""
    response = Response(b'{"code":100004,"msg":"' + b"x" * 100000)
    client = client_with(response)
    client._token = "synthetic-token"
    with pytest.raises(ProtocolError):
        await anext(client.audio_stream("synthetic-id"))
    assert len(response.content.payload) > 95000
    client = client_with(Response(b'{"code":100004}'))
    with pytest.raises(ProtocolError):
        await client.current_user()


PROFILE = ProtocolProfile("/music/api/v1", "synthetic-salt", "synthetic-api-key")


@pytest.mark.parametrize(
    ("second_origin", "expected_peak"), [("http://one.invalid", 1), ("http://two.invalid", 2)]
)
async def test_login_overlap_is_limited_per_server(second_origin: str, expected_peak: int) -> None:
    """The observed server login race is avoided without serializing unrelated servers."""
    clients: list[Any] = [
        FeiNiuClient("http://one.invalid", PROFILE),
        FeiNiuClient(second_origin, PROFILE),
    ]
    active = peak = 0
    requests = []

    async def request(_method: str, _path: str, *, body: Any) -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        requests.append(body["username"])
        await asyncio.sleep(0)
        active -= 1
        return {"userToken": "synthetic-" + body["username"], "user": {"guid": body["username"]}}

    for client in clients:
        client._json = request
    users = await asyncio.gather(
        *(
            client.login(str(index), "synthetic-secret", "a" * 32)
            for index, client in enumerate(clients)
        )
    )
    assert peak == expected_peak
    assert requests == ["0", "1"]
    assert [user["guid"] for user in users] == ["0", "1"]
    assert clients[0]._token != clients[1]._token


class Body:
    """A fragmented synthetic response body."""

    def __init__(self, payload: bytes) -> None:
        """Initialize the synthetic fixture."""
        self.payload = payload

    async def read(self, size: int) -> bytes:
        # Deliberately return short chunks to emulate TCP fragmentation.
        """Return a short chunk from the synthetic response."""
        part, self.payload = self.payload[: min(size, 7)], self.payload[min(size, 7) :]
        return part

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        """Yield the remaining fragmented fixture."""
        while part := await self.read(size):
            yield part


class Response:
    """An asynchronous synthetic response context."""

    def __init__(
        self, payload: bytes, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        """Initialize the synthetic fixture."""
        self.content = Body(payload)
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    async def __aenter__(self) -> Self:
        """Enter the synthetic response context."""
        return self

    async def __aexit__(self, *_: object) -> None:
        """Exit without external resources."""


class Session:
    """A finite response queue with recorded request options."""

    def __init__(self, *responses: Response | Exception) -> None:
        """Initialize the synthetic fixture."""
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        """Return the next synthetic response or connection failure."""
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url: str, **kwargs: Any) -> Response:
        """Dispatch a synthetic stream request."""
        return self.request("GET", url, **kwargs)


def client_with(*responses: Response | Exception) -> Any:
    """Build a real client around a synthetic transport."""
    client: Any = FeiNiuClient("http://test.invalid/music/", PROFILE)
    client._session = Session(*responses)
    return client


def envelope(value: Any, code: int = 0) -> Response:
    """Wrap synthetic data in the native response envelope."""
    return Response(json.dumps({"code": code, "data": value}).encode())


@pytest.mark.parametrize("items", [[], [{"guid": "one"}, {"guid": "two"}]])
async def test_playlist_collection_is_not_remotely_paginated(items: list[dict[str, str]]) -> None:
    """An all-at-once playlist response must not be rejected or fetched repeatedly."""
    client = client_with(envelope({"list": items, "total": len(items)}))
    found = [item async for item in client.items("playlist", size=1)]
    assert found == items
    assert len(client._session.calls) == 1
    assert client._session.calls[0][1].endswith("/playlist/list")


@pytest.mark.parametrize(
    "payload",
    [
        {"list": [{"guid": "one"}], "total": 2},
        {"list": [{"guid": "one"}, {"guid": "one"}], "total": 2},
    ],
)
async def test_incomplete_playlist_collection_fails(payload: dict[str, Any]) -> None:
    """A truncated or inconsistent full collection is never reported as complete."""
    with pytest.raises(ProtocolError):
        await client_with(envelope(payload)).playlists()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (429, RateLimitError),
        (500, NetworkError),
        (302, ProtocolError),
    ],
)
@pytest.mark.parametrize("media", [False, True])
async def test_http_errors_never_echo_response(status: Any, error: Any, media: bool) -> None:
    """Http errors never echo response."""
    client = client_with(Response(b"SECRET_TOKEN_AND_PRIVATE_PATH", status))
    client._token = "synthetic-token"
    operation = anext(client.audio_stream("synthetic-id")) if media else client.current_user()
    with pytest.raises(error) as raised:
        await operation
    assert "SECRET" not in str(raised.value)
    if status in {429, 500}:
        assert raised.value.backoff_time == (60 if status == 429 else 30)
    assert len(client._session.calls) == 1
    assert client._session.calls[0][2]["allow_redirects"] is False


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (120001, AuthenticationError),
        (120002, AuthenticationError),
        (100005, NotFoundError),
        (100004, ProtocolError),
    ],
)
async def test_application_errors(code: Any, error: Any) -> None:
    """Application errors."""
    client = client_with(envelope({}, code))
    with pytest.raises(error):
        await client.current_user()


@pytest.mark.parametrize("payload", [b"<html>login</html>", b"[]", b"{}", b'{"code":0}'])
async def test_malformed_success_is_not_empty_library(payload: Any) -> None:
    """Malformed success is not empty library."""
    client = client_with(Response(payload))
    with pytest.raises(ProtocolError):
        await client.current_user()


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        aiohttp.ClientConnectorDNSError(
            aiohttp.client_reqrep.ConnectionKey("test.invalid", 80, False, True, None, None, None),
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
        ),
        aiohttp.ClientConnectorError(
            aiohttp.client_reqrep.ConnectionKey("test.invalid", 80, False, True, None, None, None),
            ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
        ),
        aiohttp.ClientConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
        aiohttp.ServerDisconnectedError(),
    ],
)
@pytest.mark.parametrize("media", [False, True])
async def test_network_failure_preserves_cause_and_backoff(failure: Any, media: bool) -> None:
    """Transport failures retain their actual diagnostic cause without adding retries."""
    client = client_with(failure)
    client._token = "synthetic-token"
    operation = anext(client.audio_stream("synthetic-id")) if media else client.current_user()
    with pytest.raises(NetworkError) as raised:
        await operation
    assert raised.value.__cause__ is failure
    assert raised.value.backoff_time == 30
    assert len(client._session.calls) == 1


@pytest.mark.parametrize(
    "payload", [b'{"private":"synthetic-value"', b'{"private":"synthetic-value\xff"}']
)
@pytest.mark.parametrize("media", [False, True])
async def test_json_decode_preserves_cause_without_echoing_body(
    payload: bytes, media: bool
) -> None:
    """JSON/Unicode errors describe the parse location without printing the response body."""
    client = client_with(Response(payload))
    client._token = "synthetic-token"
    operation = anext(client.audio_stream("synthetic-id")) if media else client.current_user()
    with pytest.raises(ProtocolError) as raised:
        await operation
    assert isinstance(raised.value.__cause__, (json.JSONDecodeError, UnicodeDecodeError))
    assert "synthetic-value" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("media", [False, True])
@pytest.mark.parametrize("malformation", ["status", "chunk", "incomplete_headers"])
async def test_actual_http_parser_does_not_echo_response_fragments(
    media: bool, malformation: str
) -> None:
    """Exercise aiohttp's real parser with private text in malformed HTTP responses."""
    secret = b"synthetic-response-credential"
    responses = {
        "status": b"HTTP/1.1 " + secret + b"\r\n\r\n",
        "chunk": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + secret + b"\r\n",
        "incomplete_headers": b"HTTP/1.1 200 OK\r\nX-Private: " + secret + b"\r\n",
    }
    handlers: set[asyncio.Task[None]] = set()

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(responses[malformation])
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(respond(reader, writer))
        handlers.add(task)
        task.add_done_callback(handlers.discard)

    async with await asyncio.start_server(connected, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with FeiNiuClient(f"http://127.0.0.1:{port}", PROFILE) as client:
            client._token = "synthetic-request-cookie"
            operation = (
                anext(client.audio_stream("synthetic-id")) if media else client.current_user()
            )
            try:
                with pytest.raises(NetworkError) as raised:
                    await operation
                original = raised.value.__context__
                assert isinstance(original, aiohttp.ClientError)
                assert secret.decode() in "".join(traceback.format_exception(original))
                rendered = "".join(traceback.format_exception(raised.value))
                assert secret.decode() not in rendered
                assert "synthetic-request-cookie" not in rendered
                assert type(original).__name__ in str(raised.value)
                assert raised.value.__cause__ is None
                assert raised.value.backoff_time == 30
            finally:
                await asyncio.gather(*handlers)


async def test_instance_authentication_and_no_cookie_jar_dependency() -> None:
    """Instance authentication and no cookie jar dependency."""
    first = client_with(
        envelope({"userToken": "synthetic-token", "user": {"guid": "user-a"}}),
        envelope({}),
    )
    second = client_with(envelope({}))
    await first.login("test", "synthetic-password", "0" * 32)
    await first.current_user()
    await second.current_user()
    login_body = first._session.calls[0][2]["data"]
    assert b"synthetic-password" not in login_body
    assert "Cookie" not in first._session.calls[0][2]["headers"]
    assert first._session.calls[1][2]["headers"]["Cookie"] == "music-token=synthetic-token"
    assert "Cookie" not in second._session.calls[0][2]["headers"]


async def test_full_pagination_and_empty_library() -> None:
    """Full pagination and empty library."""
    client = client_with()
    client.page = AsyncMock(
        side_effect=[
            {"list": [{"guid": "a"}], "total": 2},
            {"list": [{"guid": "b"}], "total": 2},
        ]
    )
    assert [item["guid"] async for item in client.items("track", 1)] == ["a", "b"]
    assert client.page.await_args_list[1].args == ("track", 2, 1)
    client.page = AsyncMock(return_value={"list": [], "total": 0})
    assert [item async for item in client.items("track")] == []


@pytest.mark.parametrize(
    "second_page",
    [
        {"list": [{"guid": "a"}], "total": 2},
        {"list": [], "total": 2},
        {"list": [{"title": "missing ID"}], "total": 2},
        {"list": [{"guid": "b"}], "total": 3},
    ],
)
async def test_pagination_refuses_silent_incomplete_sync(second_page: Any) -> None:
    """Pagination refuses silent incomplete sync."""
    client = client_with()
    client.page = AsyncMock(side_effect=[{"list": [{"guid": "a"}], "total": 2}, second_page])
    with pytest.raises(ProtocolError):
        _ = [item async for item in client.items("track", 1)]


@pytest.mark.parametrize("data", [{}, {"list": [], "total": -1}, {"list": [None], "total": 1}])
async def test_missing_page_fields(data: Any) -> None:
    """Missing page fields."""
    client = client_with(envelope(data))
    with pytest.raises(ProtocolError):
        await client.page("track", 1)


async def test_media_200_login_page_and_bounded_fragmented_audio() -> None:
    """Media 200 login page and bounded fragmented audio."""
    client = client_with(
        Response(b"<html>login</html>"),
        Response(
            b"fLaC" + b"x" * 100000,
            206,
            {"Content-Type": "audio/flac", "Content-Range": "bytes 0-1023/100004"},
        ),
    )
    summary, _ = await client.media_prefix("audio", "synthetic-guid")
    assert summary["signature"] == "html-or-json"
    summary, data = await client.media_prefix("audio", "synthetic-guid", limit=1024)
    assert summary["signature"] == "flac"
    assert len(data) == 1024
    assert summary["content_range"] == "bytes 0-1023/100004"
    assert client._session.calls[-1][2]["headers"]["Range"] == "bytes=0-1023"


async def test_media_redirect_is_reported_without_leaking_location() -> None:
    """Media redirect is reported without leaking location."""
    client = client_with(Response(b"", 302, {"Location": "http://other.invalid/?token=SECRET"}))
    summary, _ = await client.media_prefix("audio", "synthetic-guid")
    assert summary["redirect"] is True
    assert "SECRET" not in json.dumps(summary)
    assert len(client._session.calls) == 1


async def test_large_json_is_rejected() -> None:
    """Large json is rejected."""
    client = client_with(Response(b"x" * 33))
    with pytest.raises(ProtocolError, match="size limit"):
        await client._request("GET", "/user/me", limit=32)


def test_signature_is_query_order_independent_and_body_sensitive() -> None:
    """Signature is query order independent and body sensitive."""
    fixed = {"nonce": "123456", "timestamp": "1700000000000"}
    a = make_signature(PROFILE, "GET", "/path", {"size": 1, "page": 2}, "", **fixed)
    b = make_signature(PROFILE, "GET", "/path", {"page": 2, "size": 1}, "", **fixed)
    assert a == b
    assert make_signature(PROFILE, "POST", "/path", {}, "{}", **fixed) != a


def test_no_password_url_and_no_protocol_profile_repr_secrets() -> None:
    """No password url and no protocol profile repr secrets."""
    with pytest.raises(ValueError, match="credential-free"):
        FeiNiuClient("http://user:password@test.invalid", PROFILE)
    assert "synthetic-api-key" not in repr(PROFILE)
    assert classify_media(b'{"error":"expired"}') == "html-or-json"


async def test_stream_can_close_without_consuming_whole_song() -> None:
    """Early consumer cancellation must leave most synthetic audio unread."""
    response = Response(b"ID3" + b"x" * 100000)
    client = client_with(response)
    client._token = "synthetic-token"
    stream = client.audio_stream("synthetic-id")
    assert len(await anext(stream)) == 4096
    await stream.aclose()
    assert len(response.content.payload) > 90000


@pytest.mark.parametrize("status", [401, 403])
async def test_stream_authentication_error_before_first_byte(status: int) -> None:
    """No login error bytes may be fed to a player."""
    client = client_with(Response(b'{"error":"SECRET"}', status))
    client._token = "synthetic-token"
    with pytest.raises(AuthenticationError if status == 401 else PermissionDeniedError):
        await anext(client.audio_stream("synthetic-id"))


async def test_stream_rejects_html_with_http_200() -> None:
    """HTTP success alone does not identify playable media."""
    client = client_with(Response(b"<html>login</html>"))
    client._token = "synthetic-token"
    with pytest.raises(ProtocolError, match="recognized audio"):
        await anext(client.audio_stream("synthetic-id"))
