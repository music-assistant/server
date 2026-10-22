"""A flow-mode position jump moves the queue to the track the new position falls in."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "player-1"
FIRST_TRACK_STREAMED = 200


def _controller() -> PlayerQueuesController:
    """Return a flow-mode queue whose clock estimate has moved on to the second track."""
    items = [
        QueueItem(queue_id=QUEUE_ID, queue_item_id=item_id, name=item_id, duration=240)
        for item_id in ("a", "b", "c")
    ]
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = MagicMock()
    ctrl.mass = MagicMock()
    queue = PlayerQueue(
        queue_id=QUEUE_ID,
        active=True,
        display_name="P",
        available=True,
        items=len(items),
        flow_mode=True,
        current_index=1,
        current_item=items[1],
        next_item=items[2],
        elapsed_time=10,
    )
    queue_data = PlayerQueueData(queue=queue, items=items)
    queue_data.flow_mode_stream_log = [
        PlayLogEntry("a", seconds_streamed=FIRST_TRACK_STREAMED),
        PlayLogEntry("b"),
    ]
    ctrl._queue_data = {QUEUE_ID: queue_data}
    return ctrl


def _player(state: PlaybackState, stream_elapsed: float) -> Any:
    return SimpleNamespace(
        player_id=QUEUE_ID,
        type=PlayerType.PLAYER,
        state=SimpleNamespace(
            playback_state=state,
            active_source=QUEUE_ID,
            corrected_elapsed_time=stream_elapsed,
        ),
    )


def test_jump_back_into_the_previous_track_selects_it() -> None:
    """A player found to be still in the first track moves the queue back to that track."""
    ctrl = _controller()
    ctrl.on_player_elapsed_time_corrected(_player(PlaybackState.PLAYING, 150))
    queue = ctrl._queue_data[QUEUE_ID].queue
    assert queue.current_index == 0
    assert queue.current_item is not None
    assert queue.current_item.queue_item_id == "a"
    assert queue.next_item is not None
    assert queue.next_item.queue_item_id == "b"
    assert queue.elapsed_time == pytest.approx(150)


def test_jump_within_the_same_track_keeps_it() -> None:
    """A correction inside the current track only re-bases its elapsed time."""
    ctrl = _controller()
    ctrl.on_player_elapsed_time_corrected(_player(PlaybackState.PLAYING, FIRST_TRACK_STREAMED + 30))
    queue = ctrl._queue_data[QUEUE_ID].queue
    assert queue.current_index == 1
    assert queue.elapsed_time == pytest.approx(30)


def test_paused_player_position_does_not_move_the_queue() -> None:
    """A paused player's position stays untrusted: track and elapsed time are kept."""
    ctrl = _controller()
    ctrl.on_player_elapsed_time_corrected(_player(PlaybackState.PAUSED, 150))
    queue = ctrl._queue_data[QUEUE_ID].queue
    assert queue.current_index == 1
    assert queue.elapsed_time == 10
