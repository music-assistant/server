"""Tests for the Content-Length the queue streams advertise to a player."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import HttpVersion, HttpVersion10, HttpVersion11, web
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_REQUIRES_FIXED_CONTENT_LENGTH
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.helpers.audio import calculate_content_length

QUEUE_ID = "q1"
SESSION_ID = "sess1"
PLAYER_ID = "player1"
ITEM_ID = "item1"
OUTPUT_FORMAT = AudioFormat(content_type=ContentType.FLAC, sample_rate=44100)
PLACEHOLDER_LENGTH = calculate_content_length(OUTPUT_FORMAT, 12 * 3600)
MATCH_INFO = {
    "session_id": SESSION_ID,
    "queue_id": QUEUE_ID,
    "queue_item_id": ITEM_ID,
    "player_id": PLAYER_ID,
    "fmt": "flac",
}


def _queue_item(duration: int | None) -> QueueItem:
    """
    Build a queue item with resolved stream details.

    :param duration: The duration of the item, None when it is unknown.
    """
    queue_item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id=ITEM_ID, name=ITEM_ID, duration=duration
    )
    queue_item.streamdetails = StreamDetails(
        provider="local--1",
        item_id=ITEM_ID,
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        media_type=MediaType.TRACK,
        stream_type=StreamType.LOCAL_FILE,
        path="/music/item1.flac",
        duration=duration,
    )
    return queue_item


def _controller(queue_item: QueueItem, fixed_content_length: bool) -> MagicMock:
    """
    Build a bare streams controller serving one queue that holds the given item.

    :param queue_item: The queue item the player asks for.
    :param fixed_content_length: The player's fixed content length setting.
    """
    queue_data = PlayerQueueData(
        queue=MagicMock(queue_id=QUEUE_ID, display_name="Queue"),
        items=[queue_item],
        session_id=SESSION_ID,
    )
    player = MagicMock()
    player.get_config_value.side_effect = lambda key, default=None, **_kwargs: (
        fixed_content_length if key == CONF_REQUIRES_FIXED_CONTENT_LENGTH else default
    )

    ctrl = MagicMock()
    ctrl._open_item_streams = {}
    ctrl._active_output_streams = 0
    ctrl.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    ctrl._get_flow_start_item.return_value = queue_item
    ctrl.mass.player_queues.get.return_value = queue_data.queue
    ctrl.mass.player_queues.queue_data.return_value = queue_data
    ctrl.mass.player_queues.get_item.return_value = queue_item
    ctrl.mass.player_queues.flow_stream_finished.return_value = False
    ctrl.mass.players.get_player.return_value = player
    ctrl.mass.config.get_raw_core_config_value.return_value = 8
    ctrl.mass.config.get_raw_player_config_value.return_value = "disabled"
    pcm_format = AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100)
    ctrl.audio.select_pcm_format = AsyncMock(return_value=pcm_format)
    ctrl.audio.select_flow_pcm_format = AsyncMock(return_value=pcm_format)
    ctrl.audio.get_output_format = AsyncMock(return_value=OUTPUT_FORMAT)
    ctrl.audio.fade_into_pending.return_value = False
    return ctrl


async def _serve_item(ctrl: MagicMock, version: HttpVersion = HttpVersion11) -> web.StreamResponse:
    """
    Answer a player's probe of the single item stream and return the prepared response.

    :param ctrl: The streams controller.
    :param version: The HTTP version the player speaks.
    """
    request = make_mocked_request(
        "HEAD", "/single/item.flac", match_info=MATCH_INFO, version=version
    )
    return await StreamsController.serve_queue_item_stream(ctrl, request)


async def _serve_flow(ctrl: MagicMock, version: HttpVersion = HttpVersion11) -> web.StreamResponse:
    """
    Answer a player's probe of the flow stream and return the prepared response.

    :param ctrl: The streams controller.
    :param version: The HTTP version the player speaks.
    """
    request = make_mocked_request("HEAD", "/flow/item.flac", match_info=MATCH_INFO, version=version)
    return await StreamsController.serve_queue_flow_stream(ctrl, request)


async def test_a_track_with_a_duration_gets_its_own_length() -> None:
    """A player that needs a fixed length gets the length of the track itself."""
    ctrl = _controller(_queue_item(duration=180), fixed_content_length=True)

    with patch(
        "music_assistant.controllers.streams.controller.get_content_length",
        AsyncMock(return_value=1_234_567),
    ) as get_length:
        resp = await _serve_item(ctrl)

    assert resp.content_length == 1_234_567
    get_length.assert_awaited_once()
    assert get_length.await_args is not None
    assert get_length.await_args.args[2:] == (OUTPUT_FORMAT, 180)


async def test_a_track_without_a_duration_gets_the_placeholder_length() -> None:
    """A track of unknown length is announced with a length it will never reach."""
    ctrl = _controller(_queue_item(duration=None), fixed_content_length=True)

    resp = await _serve_item(ctrl)

    assert resp.content_length == PLACEHOLDER_LENGTH


async def test_a_flow_stream_gets_the_placeholder_length() -> None:
    """The flow stream of a player that needs a fixed length gets the placeholder length."""
    ctrl = _controller(_queue_item(duration=180), fixed_content_length=True)

    resp = await _serve_flow(ctrl)

    assert resp.content_length == PLACEHOLDER_LENGTH


@pytest.mark.parametrize("serve", [_serve_item, _serve_flow], ids=["item", "flow"])
@pytest.mark.parametrize("version", [HttpVersion10, HttpVersion11], ids=["http10", "http11"])
async def test_no_length_and_no_forced_chunking_without_the_setting(
    serve: Any, version: HttpVersion
) -> None:
    """Without the setting a player gets no length and chunking is left to the HTTP version."""
    ctrl = _controller(_queue_item(duration=180), fixed_content_length=False)

    resp = await serve(ctrl, version)

    assert resp.prepared
    assert resp.content_length is None
    assert not resp.chunked
