"""
Tests for the titles a single item stream carries for a player that shows them.

A radio station or live source is one stream without an end, so a player can only learn
what plays from metadata inside the stream (ICY). A player kind that shows such titles gets
them; the ICY setting of a player only applies to a queue in flow mode.
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
    _flow_handler,
    _single_item_handler,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

BASIC_INTERVAL = 16384
FULL_INTERVAL = 256000
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
    icy_setting: str = "disabled",
    shows_stream_titles: bool = False,
    asks_for_metadata: bool = True,
) -> _RecordingStreamResponse:
    """
    Serve one queue item over the single item route and return the answer.

    :param monkeypatch: The fixture that stands in for the encoder and the response.
    :param media_type: The media type of the queue item.
    :param icy_setting: The ICY metadata setting of the player.
    :param shows_stream_titles: Whether the player shows the titles sent inside a stream.
    :param asks_for_metadata: Whether the player asks for metadata in the stream.
    """
    controller, request, _ = _single_item_handler(is_realtime=True, capture_ffmpeg=monkeypatch)
    controller._active_output_streams = 0
    controller._audio_analysis = MagicMock()
    controller._audio_analysis.shown_stream_metadata.return_value = StreamMetadata(
        title="Song", artist="Band"
    )
    controller.mass.config.get_protocol_value.return_value = icy_setting
    protocol = controller.mass.players.get_output_protocol.return_value
    protocol.shows_stream_titles = shows_stream_titles
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
@pytest.mark.parametrize("icy_setting", ["disabled", "full"])
async def test_an_item_without_an_end_carries_its_titles_for_a_player_that_shows_them(
    monkeypatch: pytest.MonkeyPatch, media_type: MediaType, icy_setting: str
) -> None:
    """The title of what the item plays is framed in, without the image, whatever the setting."""
    response = await _serve_item(
        monkeypatch, media_type, icy_setting=icy_setting, shows_stream_titles=True
    )

    assert response.headers["icy-metaint"] == str(BASIC_INTERVAL)
    # the name of the item itself, not the one of the server
    assert response.headers["icy-name"] == "Track"
    title = b"StreamTitle='Band - Song';"
    block = bytes([2]) + title.ljust(32, b"\x00")
    assert response.body == AUDIO[:BASIC_INTERVAL] + block + AUDIO[BASIC_INTERVAL:]


@pytest.mark.parametrize(
    ("media_type", "icy_setting", "shows_stream_titles", "asks_for_metadata"),
    [
        (MediaType.TRACK, "basic", True, True),
        (MediaType.RADIO, "basic", False, True),
        (MediaType.RADIO, "full", False, True),
        (MediaType.RADIO, "basic", True, False),
    ],
    ids=["a_track", "setting_basic", "setting_full", "player_does_not_ask"],
)
async def test_a_stream_without_titles_is_the_audio_alone(
    monkeypatch: pytest.MonkeyPatch,
    media_type: MediaType,
    icy_setting: str,
    shows_stream_titles: bool,
    asks_for_metadata: bool,
) -> None:
    """A track, a player that shows no titles (whatever its setting) and one that does not ask."""
    response = await _serve_item(
        monkeypatch,
        media_type,
        icy_setting=icy_setting,
        shows_stream_titles=shows_stream_titles,
        asks_for_metadata=asks_for_metadata,
    )

    assert "icy-metaint" not in response.headers
    assert response.body == AUDIO


@pytest.mark.parametrize("shows_stream_titles", [True, False])
@pytest.mark.parametrize(
    ("icy_setting", "interval"),
    [("disabled", None), ("basic", BASIC_INTERVAL), ("full", FULL_INTERVAL)],
)
async def test_a_flow_stream_follows_the_setting_alone(
    monkeypatch: pytest.MonkeyPatch,
    icy_setting: str,
    interval: int | None,
    shows_stream_titles: bool,
) -> None:
    """A queue in flow mode carries titles by the ICY setting, whatever the player shows."""
    controller, request, _ = _flow_handler(monkeypatch, is_realtime=False)
    controller._audio_analysis = MagicMock()
    controller._audio_analysis.shown_stream_metadata.return_value = None
    controller.mass.config.get_protocol_value.return_value = icy_setting
    protocol = controller.mass.players.get_output_protocol.return_value
    protocol.shows_stream_titles = shows_stream_titles
    request.headers = {"Icy-MetaData": "1"}

    async def _encoded_audio(**_kwargs: Any) -> AsyncGenerator[bytes]:
        yield AUDIO

    monkeypatch.setattr(controller_mod, "get_ffmpeg_stream", _encoded_audio)
    monkeypatch.setattr(
        controller_mod,
        "web",
        SimpleNamespace(StreamResponse=_RecordingStreamResponse, HTTPNotFound=web.HTTPNotFound),
    )

    response = await controller.serve_queue_flow_stream(request)

    assert response.headers.get("icy-metaint") == (str(interval) if interval else None)
    if interval == BASIC_INTERVAL:
        block = bytes([2]) + b"StreamTitle='Track';".ljust(32, b"\x00")
        assert response.body == AUDIO[:BASIC_INTERVAL] + block + AUDIO[BASIC_INTERVAL:]
    else:
        # the interval of the profile with the image lies past this audio
        assert response.body == AUDIO
