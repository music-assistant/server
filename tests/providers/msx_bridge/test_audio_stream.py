"""HTTP framing must agree with the output of the actual MA encoder."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from music_assistant_models.player import PlayerMedia

from music_assistant.helpers.ffmpeg import get_ffmpeg_stream
from music_assistant.providers.msx_bridge.audio_stream import build_audio_params
from music_assistant.providers.msx_bridge.http_server import MSXHTTPServer

if TYPE_CHECKING:
    from music_assistant.providers.msx_bridge.player import MSXPlayer
    from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


@pytest.mark.parametrize("codec", ["mp3", "aac", "flac"])
@pytest.mark.parametrize("include_length", [False, True])
async def test_encoded_output_never_advertises_an_estimated_size(
    codec: str, include_length: bool
) -> None:
    """One second of PCM has a variable encoded size, including codec headers and padding."""
    pcm, encoded, headers = build_audio_params(codec, 1, include_content_length=include_length)

    async def source() -> AsyncGenerator[bytes]:
        yield bytes(44100 * 2 * 2)

    body = b"".join(
        [
            chunk
            async for chunk in get_ffmpeg_stream(
                audio_input=source(),
                input_format=pcm,
                output_format=encoded,
            )
        ]
    )
    assert len(body) > 0
    if "Content-Length" in headers:
        assert int(headers["Content-Length"]) == len(body)


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
    pcm, encoded, headers = build_audio_params(codec, 1, include_content_length=True)

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
