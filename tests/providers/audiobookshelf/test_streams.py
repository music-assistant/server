"""Tests for streaming from the Audiobookshelf provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
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

    app = web.Application()
    app.router.add_get("/api/items/book1/file/1", _serve_part)
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


@pytest.mark.parametrize(
    ("range_header", "status", "body"),
    [(None, 200, PART), ("bytes=1000-", 206, PART[1000:])],
)
async def test_part_is_proxied_without_token(
    stream_provider: Audiobookshelf,
    abs_server: TestServer,
    range_header: str | None,
    status: int,
    body: bytes,
) -> None:
    """The part is streamed through, so the token never reaches the caller."""
    app = web.Application()
    app.router.add_get("/part", stream_provider._handle_session_part_request)
    headers = {hdrs.RANGE: range_header} if range_header else {}

    async with ClientSession() as http_session, TestClient(TestServer(app)) as client:
        stream_provider.mass.http_session = http_session  # type: ignore[misc]
        with patch.object(
            stream_provider,
            "get_setup_value",
            return_value=str(abs_server.make_url("")).rstrip("/"),
        ):
            response = await client.get(
                "/part", params={"session_id": "session1", "part_id": "0"}, headers=headers
            )
            assert response.status == status
            assert await response.read() == body
