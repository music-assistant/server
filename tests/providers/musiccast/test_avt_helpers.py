"""Tests for the MusicCast UPnP AVTransport requests."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp import web

from music_assistant.providers.musiccast import avt_helpers

if TYPE_CHECKING:
    from aiohttp.test_utils import TestServer

MEDIA_INFO = "<CurrentURI>http://ma:8097/A___main/track.flac</CurrentURI>"


async def _serve(
    aiohttp_server: Callable[..., Awaitable[TestServer]],
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> None:
    app = web.Application()
    app.router.add_post("/AVTransport/ctrl", handler)
    server = await aiohttp_server(app)
    url = str(server.make_url("/AVTransport/ctrl"))
    monkeypatch.setattr(avt_helpers, "get_upnp_ctrl_url", lambda _: url)


@pytest.mark.parametrize(
    ("status", "expected_media_info"),
    [
        pytest.param(200, MEDIA_INFO, id="ok"),
        pytest.param(500, None, id="error"),
    ],
)
async def test_get_media_info(
    aiohttp_server: Callable[..., Awaitable[TestServer]],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_media_info: str | None,
) -> None:
    """Media info returns the response body and raises on an error response."""

    async def handler(_: web.Request) -> web.Response:
        return web.Response(status=status, text=MEDIA_INFO)

    await _serve(aiohttp_server, monkeypatch, handler)
    async with aiohttp.ClientSession() as session:
        if expected_media_info is None:
            with pytest.raises(aiohttp.ClientError):
                await avt_helpers.avt_get_media_info(session, MagicMock())
        else:
            assert await avt_helpers.avt_get_media_info(session, MagicMock()) == expected_media_info


async def test_commands_release_connection_without_reading_body(
    aiohttp_server: Callable[..., Awaitable[TestServer]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands return once the headers arrived and give their connection back."""
    release_body = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await release_body.wait()
        return response

    await _serve(aiohttp_server, monkeypatch, handler)
    connector = aiohttp.TCPConnector(limit=1)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            for command in (avt_helpers.avt_stop, avt_helpers.avt_play):
                await asyncio.wait_for(command(session, MagicMock()), timeout=2)
    finally:
        release_body.set()
