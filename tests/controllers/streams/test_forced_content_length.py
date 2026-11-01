"""Tests for the content length the item stream announces with a forced content length."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from aiohttp.test_utils import make_mocked_request
from music_assistant_models.enums import ContentType, CrossfadeMode, MediaType, StreamType
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.streams.controller import StreamsController

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

QUEUE_ID = "q1"
SESSION_ID = "sess1"
PLAYER_ID = "player1"
ITEM_ID = "item1"
DURATION = 180
ENCODED = b"\0" * 4096


def _controller(playback_speed: float = 1.0) -> MagicMock:
    """
    Build a bare streams controller serving one queue item to a forced content length player.

    :param playback_speed: The playback speed of the queue item.
    """
    queue_item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id=ITEM_ID, name=ITEM_ID, duration=DURATION
    )
    queue_item.extra_attributes["playback_speed"] = playback_speed
    queue_item.streamdetails = StreamDetails(
        provider="local--1",
        item_id=ITEM_ID,
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        media_type=MediaType.TRACK,
        stream_type=StreamType.LOCAL_FILE,
        path="/music/item1.flac",
        duration=DURATION,
    )
    queue_data = PlayerQueueData(
        queue=MagicMock(queue_id=QUEUE_ID, display_name="Queue"),
        items=[queue_item],
        session_id=SESSION_ID,
    )
    player = MagicMock()
    player.stop_called = False
    player.get_config_value.return_value = "forced_content_length"

    ctrl = MagicMock()
    ctrl._open_item_streams = {}
    ctrl._ended_item_streams = {}
    ctrl._active_output_streams = 0
    ctrl._requested_item_ids.return_value = set()
    ctrl._release_abandoned_item_sources = AsyncMock()
    ctrl.get_crossfade_mode.return_value = CrossfadeMode.DISABLED
    ctrl.mass.player_queues.get.return_value = queue_data.queue
    ctrl.mass.player_queues.queue_data.return_value = queue_data
    ctrl.mass.player_queues.get_item.return_value = queue_item
    ctrl.mass.players.get_player.return_value = player
    ctrl.mass.config.get_raw_core_config_value.return_value = 8
    ctrl.mass.create_task.side_effect = lambda coro, **_kwargs: coro.close()
    ctrl.audio.select_pcm_format = AsyncMock(
        return_value=AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100)
    )
    ctrl.audio.get_output_format = AsyncMock(
        return_value=AudioFormat(content_type=ContentType.FLAC, sample_rate=44100, bit_depth=24)
    )
    ctrl.audio.fade_into_pending.return_value = False
    return ctrl


async def _serve(ctrl: MagicMock, announced: int) -> tuple[Any, AsyncMock]:
    """
    Serve the item to a player that reads it whole.

    :param ctrl: The streams controller.
    :param announced: The content length get_content_length returns.
    :return: The response and the get_content_length mock.
    """
    writer = Mock()
    writer.write_headers = AsyncMock()
    writer.write = AsyncMock()
    writer.write_eof = AsyncMock()
    writer.drain = AsyncMock()
    request = make_mocked_request(
        "GET",
        f"/single/{SESSION_ID}/{QUEUE_ID}/{ITEM_ID}/{PLAYER_ID}.flac",
        match_info={
            "session_id": SESSION_ID,
            "queue_id": QUEUE_ID,
            "queue_item_id": ITEM_ID,
            "player_id": PLAYER_ID,
            "fmt": "flac",
        },
        writer=writer,
    )

    async def _encoded(**_kwargs: Any) -> AsyncGenerator[bytes]:
        yield ENCODED

    content_length = AsyncMock(return_value=announced)
    with (
        patch("music_assistant.controllers.streams.controller.get_ffmpeg_stream", _encoded),
        patch("music_assistant.controllers.streams.controller.get_content_length", content_length),
    ):
        resp = await StreamsController.serve_queue_item_stream(ctrl, request)
    return resp, content_length


@pytest.mark.asyncio
async def test_a_slower_playback_speed_announces_the_longer_stream() -> None:
    """At half speed the stream lasts twice the duration, and its length is sized for that."""
    _resp, content_length = await _serve(_controller(playback_speed=0.5), len(ENCODED))
    assert content_length.call_args.args[3] == DURATION / 0.5
    assert content_length.call_args.args[4] == 0.5


@pytest.mark.asyncio
async def test_a_body_shorter_than_announced_closes_the_connection() -> None:
    """The player learns the end of a body shorter than its length from the closed connection."""
    resp, _content_length = await _serve(_controller(), len(ENCODED) * 2)
    assert resp.keep_alive is False


@pytest.mark.asyncio
async def test_a_body_of_the_announced_length_keeps_the_connection() -> None:
    """A body that reaches its announced length ends as usual."""
    resp, _content_length = await _serve(_controller(), len(ENCODED))
    assert resp.keep_alive is True
