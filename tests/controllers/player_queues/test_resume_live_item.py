"""Tests for resuming a queue on an item that has no position to seek to."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "player_1"
ITEM_ID = "item-1"


def _playing_controller(media_type: MediaType) -> PlayerQueuesController:
    """
    Build a controller whose queue plays one item of the given media type for a while.

    :param media_type: Media type of the item that plays.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(
        queue_id=QUEUE_ID, active=True, display_name="Player 1", available=True, items=1
    )
    item = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=ITEM_ID,
        name="Item",
        duration=None,
        media_item=cast("Any", SimpleNamespace(media_type=media_type)),
    )
    queue.current_item = item
    queue.current_index = 0
    queue.state = PlaybackState.PLAYING
    queue.elapsed_time = 300
    queue.elapsed_time_last_updated = time.time()
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = [item]
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.get_item = Mock(return_value=item)  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    player = MagicMock()
    player.offline_by_power_control = False
    player.playback_state = PlaybackState.PLAYING
    player.extra_data = {ATTR_ANNOUNCEMENT_IN_PROGRESS: False}
    ctrl.mass.players.get_player = Mock(return_value=player)
    ctrl.logger = MagicMock()
    return ctrl


@pytest.mark.parametrize("media_type", [MediaType.RADIO, MediaType.AUDIO_SOURCE])
async def test_a_live_item_resumes_from_its_live_point(media_type: MediaType) -> None:
    """A radio station or live source has no position to seek to, so it resumes from 0."""
    ctrl = _playing_controller(media_type)

    await ctrl.resume(QUEUE_ID)

    ctrl.play_index.assert_awaited_once_with(QUEUE_ID, ITEM_ID, 0, False)  # type: ignore[attr-defined]


async def test_a_track_resumes_where_it_plays() -> None:
    """A track that plays resumes at its position."""
    ctrl = _playing_controller(MediaType.TRACK)

    await ctrl.resume(QUEUE_ID)

    position = ctrl.play_index.await_args.args[2]  # type: ignore[attr-defined]
    assert position >= 300
