"""HTTP framing must agree with the output of the actual MA encoder."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from music_assistant_models.player import PlayerMedia

from music_assistant.controllers.streams.constants import PacingProfile
from music_assistant.providers.msx_bridge.audio_stream import build_audio_params
from music_assistant.providers.msx_bridge.constants import PRE_BUFFER_BYTES
from music_assistant.providers.msx_bridge.http_server import MSXHTTPServer

if TYPE_CHECKING:
    from music_assistant.providers.msx_bridge.player import MSXPlayer
    from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


@pytest.mark.parametrize("codec", ["mp3", "aac", "flac"])
async def test_independent_http_body_reaches_real_encoder_eof(
    provider: MSXBridgeProvider,
    player: MSXPlayer,
    mass_mock: Mock,
    codec: str,
) -> None:
    """The product HTTP producer delivers the entire encoded body without a guessed size."""
    server = MSXHTTPServer(provider, 0)
    media = PlayerMedia(uri="http://synthetic", duration=1)
    await player.play_media(media)

    async def source(*_args: Any, **_kwargs: Any) -> AsyncGenerator[bytes]:
        yield bytes(44100 * 2 * 2)

    mass_mock.streams.get_stream = source
    mass_mock.streams.audio.get_player_output_plan.return_value = SimpleNamespace(filter_params=[])
    pcm, encoded, headers = build_audio_params(codec)

    async def serve(request: web.Request) -> web.StreamResponse:
        return await server.audio.serve_independent(request, player, media, pcm, encoded, headers)

    app = web.Application()
    app.router.add_get("/synthetic", serve)
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/synthetic")
        body = await response.read()
        assert response.status == 200
        assert response.headers.get("Transfer-Encoding") == "chunked"
        assert "Content-Length" not in response.headers
        assert len(body) > 0
    assert not server.audio.active_stream_tasks
    assert not server.audio.active_stream_transports


async def test_cancelled_stream_closes_the_encoder_stream(
    provider: MSXBridgeProvider,
    player: MSXPlayer,
) -> None:
    """Cancelling a stream closes the ffmpeg stream while the producer waits on a full buffer."""
    server = MSXHTTPServer(provider, 0)
    pcm, encoded, _headers = build_audio_params("mp3")
    closed = asyncio.Event()
    buffer_full = asyncio.Event()
    produced = 0
    # holding a reference keeps garbage collection from closing an abandoned stream
    streams: list[AsyncGenerator[bytes]] = []

    async def produce() -> AsyncGenerator[bytes]:
        nonlocal produced
        try:
            while True:
                produced += 1
                if produced > 33:
                    buffer_full.set()
                yield bytes(PRE_BUFFER_BYTES)
        finally:
            closed.set()

    def encoder(**_kwargs: Any) -> AsyncGenerator[bytes]:
        streams.append(produce())
        return streams[-1]

    async def never_prepared(_request: web.Request) -> None:
        await asyncio.Event().wait()

    response = Mock()
    response.prepare = AsyncMock(side_effect=never_prepared)
    with patch("music_assistant.providers.msx_bridge.audio_stream.get_ffmpeg_stream", encoder):
        stream_task = asyncio.create_task(
            server.audio.stream_with_prebuffer(
                make_mocked_request("GET", "/msx/audio/msx_test"),
                response,
                player,
                None,
                pcm,
                encoded,
                [],
                PacingProfile.DEFAULT,
            )
        )
        await asyncio.wait_for(buffer_full.wait(), timeout=2)
        stream_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stream_task

    assert closed.is_set()
