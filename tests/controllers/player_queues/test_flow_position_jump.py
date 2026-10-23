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


def _played_120_s_into_the_second_track(ctrl: PlayerQueuesController) -> PlayerQueue:
    queue_data = ctrl._queue_data[QUEUE_ID]
    queue_data.prev_state = {"current_item_id": "b", "last_playing_elapsed_time": 120}  # type: ignore[typeddict-item]
    queue_data.queue.elapsed_time = 120
    # MA's own pause parks its clock estimate as the resume position.
    queue_data.queue.resume_pos = 120
    return queue_data.queue


def test_backward_correction_moves_the_resume_position_back() -> None:
    """Resuming after a correction to 90 s starts at 90 s, not the estimate of 120 s."""
    ctrl = _controller()
    queue = _played_120_s_into_the_second_track(ctrl)
    ctrl.on_player_elapsed_time_corrected(_player(PlaybackState.PLAYING, FIRST_TRACK_STREAMED + 90))
    assert queue.resume_pos == 90
    queue.resume_pos = 0  # after the parked position is used once
    assert ctrl._last_played_position(queue) == pytest.approx(90)


def test_paused_position_leaves_the_resume_position_alone() -> None:
    """Players that reset their position when paused must not move the resume position."""
    ctrl = _controller()
    queue = _played_120_s_into_the_second_track(ctrl)
    ctrl.on_player_elapsed_time_corrected(_player(PlaybackState.PAUSED, 0))
    assert queue.resume_pos == 120
    assert ctrl._queue_data[QUEUE_ID].prev_state["last_playing_elapsed_time"] == 120  # type: ignore[index]
