"""Tests for streaming from the Audiobookshelf provider."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioaudiobookshelf.exceptions import ServiceUnavailableError as AbsServiceUnavailableError
from aioaudiobookshelf.exceptions import SessionNotFoundError as AbsSessionNotFoundError
from aiohttp import ClientSession, hdrs, web
from aiohttp.test_utils import TestClient, TestServer
from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from music_assistant.providers.audiobookshelf import Audiobookshelf

PART = bytes(range(256)) * 4


@pytest.fixture
def stream_provider(provider: Audiobookshelf) -> Audiobookshelf:
    """Return a provider whose abs server opens a session with one part."""
    provider.create_session_lock = asyncio.Lock()
    provider.sessions = {}
    provider.mass.streams = Mock(base_url="http://ma.local:8097")
    track = Mock(duration=60, metadata=None, content_url="/api/items/book1/file/1")
    session = Mock(id_="session1", duration=60, audio_tracks=[track])
    provider._client.get_playback_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    provider._client.get_open_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    provider._client.session_config = Mock(
        headers={hdrs.AUTHORIZATION: "Bearer secret"}, verify_ssl=True
    )
    return provider


@pytest.fixture
async def abs_server() -> AsyncIterator[TestServer]:
    """Serve a part like abs does: only with the token header, honoring a range."""

    async def _serve_part(request: web.Request) -> web.Response:
        if request.headers.get(hdrs.AUTHORIZATION) != "Bearer secret":
            return web.Response(status=401)
        if not (range_header := request.headers.get(hdrs.RANGE)):
            return web.Response(body=PART, headers={hdrs.ACCEPT_RANGES: "bytes"})
        start = int(range_header.removeprefix("bytes=").removesuffix("-"))
        return web.Response(
            status=206,
            body=PART[start:],
            headers={hdrs.CONTENT_RANGE: f"bytes {start}-{len(PART) - 1}/{len(PART)}"},
        )

    async def _serve_endless(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        with suppress(ConnectionError):
            while True:
                await response.write(PART)
        return response

    async def _serve_dropped(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={hdrs.CONTENT_LENGTH: str(2 * len(PART))})
        await response.prepare(request)
        await response.write(PART)
        assert request.transport is not None
        request.transport.close()
        return response

    app = web.Application()
    app.router.add_get("/api/items/book1/file/1", _serve_part)
    app.router.add_get("/endless", _serve_endless)
    app.router.add_get("/dropped", _serve_dropped)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


async def test_stream_details(stream_provider: Audiobookshelf) -> None:
    """Stream details carry the item id and point at our part proxy, not at abs."""
    with patch.object(stream_provider, "get_setup_value", return_value="http://abs.local"):
        details = await stream_provider.get_stream_details("book1", MediaType.AUDIOBOOK)

    # the item id, so loudness measured once is reused
    assert details.item_id == "book1"
    assert details.path == (
        "http://ma.local:8097/audiobookshelf--test123_part_stream?session_id=session1&part_id=0"
    )


async def test_device_ids_reused(stream_provider: Audiobookshelf) -> None:
    """Parallel sessions get distinct device ids, and a freed one is taken again."""
    create = stream_provider._client.get_playback_session

    def _device_id() -> str:
        return str(create.call_args.kwargs["session_parameters"].device_info.device_id)  # type: ignore[attr-defined]

    await stream_provider._get_playback_session("book1")
    assert _device_id() == "audiobookshelf--test123_0"
    await stream_provider._get_playback_session("book2")
    assert _device_id() == "audiobookshelf--test123_1"

    stream_provider.sessions.pop("book1")
    await stream_provider._get_playback_session("book3")
    assert _device_id() == "audiobookshelf--test123_0"

    # a session closed by abs is reopened on its own device
    stream_provider._client.get_open_session.side_effect = AbsSessionNotFoundError  # type: ignore[attr-defined]
    await stream_provider._get_playback_session("book2")
    assert _device_id() == "audiobookshelf--test123_1"


@pytest.fixture
async def part_proxy(
    stream_provider: Audiobookshelf, abs_server: TestServer
) -> AsyncIterator[tuple[TestClient[web.Request, web.Application], list[object]]]:
    """Serve the part handler against the fake abs, recording what each request ended with."""
    outcomes: list[object] = []

    async def _handle(request: web.Request) -> web.StreamResponse:
        try:
            response = await stream_provider._handle_session_part_request(request)
        except BaseException as err:
            outcomes.append(err)
            raise
        outcomes.append(response)
        return response

    app = web.Application()
    app.router.add_get("/part", _handle)
    async with ClientSession() as http_session, TestClient(TestServer(app)) as client:
        stream_provider.mass.http_session = http_session  # type: ignore[misc]
        with patch.object(
            stream_provider,
            "get_setup_value",
            return_value=str(abs_server.make_url("")).rstrip("/"),
        ):
            yield client, outcomes


@pytest.mark.parametrize(
    ("range_header", "status", "body"),
    [(None, 200, PART), ("bytes=1000-", 206, PART[1000:])],
)
async def test_part_is_proxied_without_token(
    part_proxy: tuple[TestClient[web.Request, web.Application], list[object]],
    range_header: str | None,
    status: int,
    body: bytes,
) -> None:
    """The part is streamed through, so the token never reaches the caller."""
    client, _ = part_proxy
    headers = {hdrs.RANGE: range_header} if range_header else {}
    response = await client.get(
        "/part", params={"session_id": "session1", "part_id": "0"}, headers=headers
    )
    assert response.status == status
    assert await response.read() == body


@pytest.mark.parametrize("abs_path", ["/endless", "/dropped"])
async def test_part_ends_cleanly_on_hang_up(
    stream_provider: Audiobookshelf,
    part_proxy: tuple[TestClient[web.Request, web.Application], list[object]],
    abs_path: str,
) -> None:
    """A client or abs hanging up mid part ends the request without an error."""
    client, outcomes = part_proxy
    session = stream_provider._client.get_open_session.return_value  # type: ignore[attr-defined]
    session.audio_tracks[0].content_url = abs_path
    response = await client.get("/part", params={"session_id": "session1", "part_id": "0"})
    # ffmpeg hangs up e.g. when it seeks
    await response.content.readexactly(len(PART))
    response.close()

    async with asyncio.timeout(5):
        while not outcomes:
            await asyncio.sleep(0.01)
    assert isinstance(outcomes[0], web.StreamResponse)


@pytest.mark.parametrize("failing_call", ["open_session", "part"])
async def test_part_abs_unreachable(
    stream_provider: Audiobookshelf,
    part_proxy: tuple[TestClient[web.Request, web.Application], list[object]],
    failing_call: str,
) -> None:
    """An unreachable abs is a bad gateway, not a server error."""
    client, _ = part_proxy
    if failing_call == "open_session":
        stream_provider._client.get_open_session.side_effect = (  # type: ignore[attr-defined]
            AbsServiceUnavailableError
        )
    with patch.object(stream_provider, "get_setup_value", return_value="http://127.0.0.1:1"):
        response = await client.get("/part", params={"session_id": "session1", "part_id": "0"})
    assert response.status == 502
