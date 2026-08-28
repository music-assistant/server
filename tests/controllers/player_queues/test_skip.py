"""Tests for the relative skip command on the player queues controller."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import InvalidCommand
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"
DURATION = 3600


def _controller(
    *,
    elapsed_time: float = 100.0,
    anchor_age: float = 2.0,
    state: PlaybackState = PlaybackState.PLAYING,
    playback_speed: float = 1.0,
    duration: int | None = DURATION,
) -> tuple[PlayerQueuesController, PlayerQueue, AsyncMock]:
    """Build a bare controller playing a single audiobook-sized item, with seek stubbed out."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=1)
    item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id="item1", name="chapter one", duration=duration
    )
    queue.current_index = 0
    queue.current_item = item
    queue.state = state
    queue.playback_speed = playback_speed
    queue.elapsed_time = elapsed_time
    queue.elapsed_time_last_updated = time.time() - anchor_age
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = [item]
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player_lock = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    seek = AsyncMock()
    ctrl.seek = seek  # type: ignore[method-assign]
    return ctrl, queue, seek


def _seeked_position(seek: AsyncMock) -> int:
    """Return the absolute position the stubbed seek was called with."""
    seek.assert_awaited_once()
    assert seek.await_args is not None
    position: int = seek.await_args.args[1]
    return position


async def test_skip_uses_live_position_while_playing() -> None:
    """The queue clock only ticks once a second, so a skip must work out the position now."""
    ctrl, _queue, seek = _controller(elapsed_time=100.0, anchor_age=2.0)

    await ctrl.skip(QUEUE_ID, 10)

    # reading the stored anchor directly would give 110 and lose the two elapsed seconds
    assert _seeked_position(seek) == pytest.approx(112, abs=1)


async def test_skip_accounts_for_playback_speed() -> None:
    """At 1.5x an audiobook has covered more of itself than wall clock time suggests."""
    ctrl, _queue, seek = _controller(elapsed_time=100.0, anchor_age=2.0, playback_speed=1.5)

    await ctrl.skip(QUEUE_ID, 10)

    assert _seeked_position(seek) == pytest.approx(113, abs=1)


async def test_skip_while_paused_uses_the_stored_position() -> None:
    """A paused queue's anchor is not stale, so its age must not be added to the target."""
    ctrl, _queue, seek = _controller(
        elapsed_time=100.0, anchor_age=900.0, state=PlaybackState.PAUSED
    )

    await ctrl.skip(QUEUE_ID, 10)

    assert _seeked_position(seek) == 110


async def test_skip_forward_near_the_end_clamps_instead_of_raising() -> None:
    """Pressing skip forward in the last seconds of a chapter must not error out."""
    ctrl, _queue, seek = _controller(elapsed_time=DURATION - 2, anchor_age=0.0)

    await ctrl.skip(QUEUE_ID, 30)

    assert _seeked_position(seek) == DURATION - 1


async def test_skip_back_past_the_start_clamps_to_zero() -> None:
    """Skipping back further than the current position lands at the start of the item."""
    ctrl, _queue, seek = _controller(elapsed_time=5.0, anchor_age=0.0)

    await ctrl.skip(QUEUE_ID, -30)

    assert _seeked_position(seek) == 0


async def test_skip_requires_an_item_with_a_duration() -> None:
    """Radio and not-yet-probed items have no range to skip within."""
    ctrl, _queue, seek = _controller(duration=None)

    with pytest.raises(InvalidCommand, match="without duration"):
        await ctrl.skip(QUEUE_ID, 10)

    seek.assert_not_awaited()


async def test_skip_requires_an_active_queue() -> None:
    """An inactive queue has no playback position to move."""
    ctrl, queue, seek = _controller()
    queue.active = False

    with pytest.raises(InvalidCommand, match="not active"):
        await ctrl.skip(QUEUE_ID, 10)

    seek.assert_not_awaited()


async def test_previous_restarts_the_track_when_past_the_threshold() -> None:
    """A stale anchor must not make a 6s-in previous jump back to the preceding track."""
    ctrl, queue, _seek = _controller(elapsed_time=4.5, anchor_age=2.0)
    queue.current_index = 1
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.get_item = Mock(return_value=queue.current_item)  # type: ignore[method-assign]

    await ctrl.previous(QUEUE_ID)

    # corrected position is 6.5s, so the current track restarts rather than stepping back
    assert queue.current_index == 1
