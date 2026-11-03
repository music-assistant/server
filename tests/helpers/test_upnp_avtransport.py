"""Tests for the UPnP AVTransport requests."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import aiohttp
import pytest
from aiohttp import web
from music_assistant_models.enums import MediaType

from music_assistant.helpers.upnp import AVTransport
from music_assistant.models.player import PlayerMedia

if TYPE_CHECKING:
    from aiohttp.test_utils import TestServer

MEDIA_INFO = "<CurrentURI>http://ma:8097/A___main/track.flac</CurrentURI>"


async def _serve(
    aiohttp_server: Callable[..., Awaitable[TestServer]],
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> str:
    app = web.Application()
    app.router.add_post("/AVTransport/ctrl", handler)
    server = await aiohttp_server(app)
    return str(server.make_url("/AVTransport/ctrl"))


@pytest.mark.parametrize(
    ("status", "expected_media_info"),
    [
        pytest.param(200, MEDIA_INFO, id="ok"),
        pytest.param(500, None, id="error"),
    ],
)
async def test_get_media_info(
    aiohttp_server: Callable[..., Awaitable[TestServer]],
    status: int,
    expected_media_info: str | None,
) -> None:
    """Media info returns the response body and raises on an error response."""

    async def handler(_: web.Request) -> web.Response:
        return web.Response(status=status, text=MEDIA_INFO)

    ctrl_url = await _serve(aiohttp_server, handler)
    async with aiohttp.ClientSession() as session:
        avt = AVTransport(client=session, ctrl_url=ctrl_url, user_agent="test")
        if expected_media_info is None:
            with pytest.raises(aiohttp.ClientError):
                await avt.get_media_info()
        else:
            assert await avt.get_media_info() == expected_media_info


async def test_commands_release_connection_without_reading_body(
    aiohttp_server: Callable[..., Awaitable[TestServer]],
) -> None:
    """Commands return once the headers arrived and give their connection back."""
    release_body = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await release_body.wait()
        return response

    ctrl_url = await _serve(aiohttp_server, handler)
    connector = aiohttp.TCPConnector(limit=1)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            avt = AVTransport(client=session, ctrl_url=ctrl_url, user_agent="test")
            for command in (avt.stop, avt.play):
                await asyncio.wait_for(command(), timeout=2)
    finally:
        release_body.set()


@pytest.mark.parametrize("encoding", ["utf-8", "latin-1"])
async def test_set_url_sends_complete_xml(
    aiohttp_server: Callable[..., Awaitable[TestServer]], encoding: str
) -> None:
    """A stream url with non-ASCII characters arrives as complete xml in the renderer's encoding."""
    received: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        received.append((await request.read()).decode(encoding))
        return web.Response()

    ctrl_url = await _serve(aiohttp_server, handler)
    media = PlayerMedia(uri="http://radio/stö.mp3", media_type=MediaType.RADIO)
    async with aiohttp.ClientSession() as session:
        avt = AVTransport(client=session, ctrl_url=ctrl_url, user_agent="test", encoding=encoding)
        await avt.set_url(media)

    assert "<CurrentURI>http://radio/stö.mp3</CurrentURI>" in received[0]
    assert received[0].endswith("</s:Envelope>")
