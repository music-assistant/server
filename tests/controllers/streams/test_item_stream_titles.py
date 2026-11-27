"""
Tests for the titles a single item stream carries for a player that asks for them.

A radio station or live source is one stream without an end, so a player can only learn
what plays from metadata inside the stream (ICY), as it does for a queue in flow mode.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from music_assistant_models.enums import MediaType
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.controllers.streams import controller as controller_mod
from tests.controllers.streams.test_realtime_sources import (
    _FakeStreamResponse,
    _single_item_handler,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

BASIC_INTERVAL = 16384
AUDIO = b"a" * (BASIC_INTERVAL + 1000)


class _RecordingStreamResponse(_FakeStreamResponse):
    """Keep the headers and the body the handler answers with."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.headers: dict[str, str] = kwargs["headers"]
        self.body = b""

    async def write(self, chunk: bytes) -> None:
        """Keep a written chunk."""
        self.body += chunk


async def _serve_item(
    monkeypatch: pytest.MonkeyPatch,
    media_type: MediaType,
    *,
    icy_setting: str = "basic",
    asks_for_metadata: bool = True,
) -> _RecordingStreamResponse:
    """
    Serve one queue item over the single item route and return the answer.

    :param monkeypatch: The fixture that stands in for the encoder and the response.
    :param media_type: The media type of the queue item.
    :param icy_setting: The ICY metadata setting of the player.
    :param asks_for_metadata: Whether the player asks for metadata in the stream.
    """
    controller, request, _ = _single_item_handler(is_realtime=True, capture_ffmpeg=monkeypatch)
    controller._active_output_streams = 0
    controller._audio_analysis = MagicMock()
    controller._audio_analysis.shown_stream_metadata.return_value = StreamMetadata(
        title="Song", artist="Band"
    )
    controller.mass.config.get_protocol_value.return_value = icy_setting
    queue_item = controller.mass.player_queues.get_item.return_value
    queue_item.media_type = media_type
    # the queue still plays another item, as when a player fetches its next item ahead
    controller.mass.player_queues.get.return_value.current_item = SimpleNamespace(
        name="Other", streamdetails=None, image=None
    )
    request.headers = {"Icy-MetaData": "1"} if asks_for_metadata else {}

    async def _encoded_audio(**_kwargs: Any) -> AsyncGenerator[bytes]:
        yield AUDIO[:10000]
        yield AUDIO[10000:]

    monkeypatch.setattr(controller_mod, "get_ffmpeg_stream", _encoded_audio)
    monkeypatch.setattr(
        controller_mod,
        "web",
        SimpleNamespace(StreamResponse=_RecordingStreamResponse, HTTPNotFound=web.HTTPNotFound),
    )

    response = await controller.serve_queue_item_stream(request)

    assert isinstance(response, _RecordingStreamResponse)
    return response


@pytest.mark.parametrize("media_type", [MediaType.RADIO, MediaType.AUDIO_SOURCE])
async def test_an_item_without_an_end_carries_its_titles_for_a_player_that_asks(
    monkeypatch: pytest.MonkeyPatch, media_type: MediaType
) -> None:
    """The title of what the item plays is framed into its stream at the announced interval."""
    response = await _serve_item(monkeypatch, media_type)

    assert response.headers["icy-metaint"] == str(BASIC_INTERVAL)
    # the name of the item itself, not the one of the server
    assert response.headers["icy-name"] == "Track"
    title = b"StreamTitle='Band - Song';"
    block = bytes([2]) + title.ljust(32, b"\x00")
    assert response.body == AUDIO[:BASIC_INTERVAL] + block + AUDIO[BASIC_INTERVAL:]


@pytest.mark.parametrize(
    ("media_type", "icy_setting", "asks_for_metadata"),
    [
        (MediaType.TRACK, "basic", True),
        (MediaType.RADIO, "disabled", True),
        (MediaType.RADIO, "basic", False),
    ],
    ids=["a_track", "setting_disabled", "player_does_not_ask"],
)
async def test_a_stream_without_titles_is_the_audio_alone(
    monkeypatch: pytest.MonkeyPatch,
    media_type: MediaType,
    icy_setting: str,
    asks_for_metadata: bool,
) -> None:
    """A track, a player with the setting disabled and a player that does not ask get audio only."""
    response = await _serve_item(
        monkeypatch, media_type, icy_setting=icy_setting, asks_for_metadata=asks_for_metadata
    )

    assert "icy-metaint" not in response.headers
    assert response.body == AUDIO
