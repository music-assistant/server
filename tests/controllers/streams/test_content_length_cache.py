"""Tests for the content length the item stream measures for the next play of a track."""

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
CHUNK = b"\0" * 1024
CHUNKS = 4


def _controller(queue_item: QueueItem) -> MagicMock:
    """
    Build a bare streams controller serving one queue that holds the given item.

    :param queue_item: The queue item the player asks for.
    """
    queue_data = PlayerQueueData(
        queue=MagicMock(queue_id=QUEUE_ID, display_name="Queue"),
        items=[queue_item],
        session_id=SESSION_ID,
    )
    player = MagicMock()
    player.stop_called = False
    player.get_config_value.return_value = "default"

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
    # the cache write is scheduled as a task; the test only reads what was handed over
    ctrl.mass.create_task.side_effect = lambda coro, **_kwargs: coro.close()
    ctrl.audio.select_pcm_format = AsyncMock(
        return_value=AudioFormat(content_type=ContentType.PCM_S16LE, sample_rate=44100)
    )
    ctrl.audio.get_output_format = AsyncMock(
        return_value=AudioFormat(content_type=ContentType.FLAC, sample_rate=44100)
    )
    ctrl.audio.fade_into_pending.return_value = False
    return ctrl


def _queue_item() -> QueueItem:
    """Build a queue item whose source already streamed its whole duration."""
    queue_item = QueueItem(queue_id=QUEUE_ID, queue_item_id=ITEM_ID, name=ITEM_ID, duration=180)
    queue_item.streamdetails = StreamDetails(
        provider="local--1",
        item_id=ITEM_ID,
        audio_format=AudioFormat(content_type=ContentType.FLAC),
        media_type=MediaType.TRACK,
        stream_type=StreamType.LOCAL_FILE,
        path="/music/item1.flac",
        duration=180,
    )
    queue_item.streamdetails.seconds_streamed = 180.0
    return queue_item


async def _encoded(**_kwargs: Any) -> AsyncGenerator[bytes]:
    """Yield the encoded audio of the item."""
    for _ in range(CHUNKS):
        yield CHUNK


async def _serve(ctrl: MagicMock, writes_before_drop: int | None) -> AsyncMock:
    """
    Serve the item to a player and return the cache store it was handed to.

    :param ctrl: The streams controller.
    :param writes_before_drop: Writes the player takes before it closes the connection,
        or None when it reads the whole stream.
    """
    writer = Mock()
    writer.write_headers = AsyncMock()
    writer.write_eof = AsyncMock()
    writer.drain = AsyncMock()
    writes = 0

    async def _write(_data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes_before_drop is not None and writes > writes_before_drop:
            raise ConnectionResetError("Cannot write to closing transport")

    writer.write = AsyncMock(side_effect=_write)
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
    store = AsyncMock()
    with (
        patch("music_assistant.controllers.streams.controller.get_ffmpeg_stream", _encoded),
        patch(
            "music_assistant.controllers.streams.controller.store_content_length_in_cache",
            store,
        ),
    ):
        await StreamsController.serve_queue_item_stream(ctrl, request)
    return store


@pytest.mark.asyncio
async def test_a_stream_played_to_its_end_measures_the_content_length() -> None:
    """A track the player read to its end leaves its size for the next play."""
    queue_item = _queue_item()
    store = await _serve(_controller(queue_item), writes_before_drop=None)
    store.assert_called_once()
    assert store.call_args.args[3:] == (CHUNKS * len(CHUNK), 180.0)


@pytest.mark.asyncio
async def test_a_stream_the_player_left_early_measures_nothing() -> None:
    """A track the player left partway leaves no size that would cut the next play short."""
    queue_item = _queue_item()
    store = await _serve(_controller(queue_item), writes_before_drop=2)
    store.assert_not_called()
