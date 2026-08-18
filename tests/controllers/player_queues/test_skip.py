"""Tests for the relative skip command on the player queues controller."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import InvalidCommand
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"
DURATION = 3600


class FakeTimers:
    """Stand-in for mass.call_later that debounces on task_id like the real one."""

    def __init__(self) -> None:
        """Initialize with no scheduled callbacks."""
        self.scheduled: dict[str, tuple[Callable[..., Any], tuple[Any, ...]]] = {}

    def call_later(
        self, _delay: float, target: Callable[..., Any], *args: Any, task_id: str | None = None
    ) -> None:
        """Record the callback, replacing any earlier one with the same task_id."""
        assert task_id is not None, "a debounced call must carry a task_id"
        self.scheduled[task_id] = (target, args)

    def cancel_timer(self, task_id: str) -> None:
        """Drop a scheduled callback."""
        self.scheduled.pop(task_id, None)

    async def fire(self) -> None:
        """Run every scheduled callback, as the event loop would once the delay elapses."""
        due = list(self.scheduled.values())
        self.scheduled.clear()
        for target, args in due:
            await target(*args)


def _controller(
    *,
    stub_play_index: bool = True,
    elapsed_time: float = 100.0,
    anchor_age: float = 2.0,
    state: PlaybackState = PlaybackState.PLAYING,
    playback_speed: float = 1.0,
    duration: int | None = DURATION,
    announcing: bool = False,
) -> tuple[PlayerQueuesController, PlayerQueue, FakeTimers, AsyncMock]:
    """Build a bare controller playing a single audiobook-sized item, with play_index stubbed."""
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

    timers = FakeTimers()
    ctrl.mass = MagicMock()
    ctrl.mass.call_later = Mock(side_effect=timers.call_later)
    ctrl.mass.cancel_timer = Mock(side_effect=timers.cancel_timer)
    extra_data = {ATTR_ANNOUNCEMENT_IN_PROGRESS: True} if announcing else {}
    ctrl.mass.players.get_player = Mock(return_value=MagicMock(extra_data=extra_data))
    ctrl.mass.players.play_media = AsyncMock()
    ctrl.mass.players._handle_cmd_stop = AsyncMock()
    ctrl.mass.players._handle_cmd_pause = AsyncMock()
    ctrl.mass.create_task = Mock()
    ctrl.logger = MagicMock()
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    play_index = AsyncMock()
    if stub_play_index:
        ctrl.play_index = play_index  # type: ignore[method-assign]
    return ctrl, queue, timers, play_index


def _pending_target(ctrl: PlayerQueuesController) -> float:
    """Return the accumulated target awaiting its stream restart."""
    pending = ctrl._queue_data[QUEUE_ID].pending_skip
    assert pending is not None
    return pending.target


def _restarted_at(play_index: AsyncMock) -> int:
    """Return the seek position the (single) stream restart was made with."""
    play_index.assert_awaited_once()
    assert play_index.await_args is not None
    position: int = play_index.await_args.kwargs["seek_position"]
    return position


async def test_skip_uses_live_position_while_playing() -> None:
    """The queue clock only ticks once a second, so a skip must extrapolate to now."""
    ctrl, _queue, timers, play_index = _controller(elapsed_time=100.0, anchor_age=2.0)

    await ctrl.skip(QUEUE_ID, 10)
    await timers.fire()

    # reading the stored anchor directly would give 110 and lose the two elapsed seconds
    assert _restarted_at(play_index) == pytest.approx(112, abs=1)


async def test_skip_accounts_for_playback_speed() -> None:
    """At 1.5x an audiobook has covered more media-time than wall-clock since the last update."""
    ctrl, _queue, timers, play_index = _controller(
        elapsed_time=100.0, anchor_age=2.0, playback_speed=1.5
    )

    await ctrl.skip(QUEUE_ID, 10)
    await timers.fire()

    assert _restarted_at(play_index) == pytest.approx(113, abs=1)


async def test_skip_while_paused_uses_the_stored_position() -> None:
    """A paused queue's anchor is not stale, so its age must not be added to the target."""
    ctrl, _queue, timers, play_index = _controller(
        elapsed_time=100.0, anchor_age=900.0, state=PlaybackState.PAUSED
    )

    await ctrl.skip(QUEUE_ID, 10)
    await timers.fire()

    assert _restarted_at(play_index) == 110


async def test_rapid_presses_collapse_into_one_stream_restart() -> None:
    """Three taps of a 10s back button must go back 30s, rebuilding the stream once."""
    ctrl, queue, timers, play_index = _controller(elapsed_time=100.0, anchor_age=0.0)
    published: list[float] = []
    ctrl.signal_update = Mock(  # type: ignore[method-assign]
        side_effect=lambda *_a, **_kw: published.append(queue.elapsed_time)
    )

    for _ in range(3):
        await ctrl.skip(QUEUE_ID, -10)

    # the UI tracks every press, but only one restart is queued
    assert published == pytest.approx([90, 80, 70], abs=0.1)
    assert list(timers.scheduled) == [f"queue_play_index_{QUEUE_ID}"]
    play_index.assert_not_awaited()

    await timers.fire()

    assert _restarted_at(play_index) == 70


async def test_press_during_the_window_ignores_the_frozen_clock() -> None:
    """A follow-up press composes onto the pending target, not the extrapolating queue clock."""
    ctrl, queue, _timers, _play_index = _controller(elapsed_time=100.0, anchor_age=0.0)

    await ctrl.skip(QUEUE_ID, -10)
    # the first press republished the clock, which keeps running while the queue is frozen
    queue.elapsed_time_last_updated = time.time() - 3
    await ctrl.skip(QUEUE_ID, -10)

    # composing off the frozen clock instead would give ~77
    assert _pending_target(ctrl) == pytest.approx(80, abs=0.1)


async def test_a_stale_target_is_not_composed_onto() -> None:
    """A target left behind by a burst that never applied must not move a later skip."""
    ctrl, queue, _timers, _play_index = _controller(elapsed_time=100.0, anchor_age=0.0)

    await ctrl.skip(QUEUE_ID, -30)
    # something cancelled the burst without clearing its target, and the same item plays on
    pending = ctrl._queue_data[QUEUE_ID].pending_skip
    assert pending is not None
    ctrl._queue_data[QUEUE_ID].pending_skip = replace(pending, set_at=time.time() - 60)
    queue.elapsed_time = 100.0
    queue.elapsed_time_last_updated = time.time()

    await ctrl.skip(QUEUE_ID, -10)

    # composing onto the stale target would give 60
    assert _pending_target(ctrl) == pytest.approx(90, abs=0.1)


async def test_skip_freezes_queue_updates_until_applied() -> None:
    """Without this the once-a-second poll would snap the position back before the restart."""
    ctrl, _queue, _timers, _play_index = _controller()

    await ctrl.skip(QUEUE_ID, -10)

    assert ctrl._queue_data[QUEUE_ID].transitioning is True


async def test_skip_forward_near_the_end_clamps_instead_of_raising() -> None:
    """Pressing skip forward in the last seconds of a chapter must not error out."""
    ctrl, _queue, timers, play_index = _controller(elapsed_time=DURATION - 2, anchor_age=0.0)

    await ctrl.skip(QUEUE_ID, 30)
    await timers.fire()

    assert _restarted_at(play_index) == DURATION - 1


async def test_skip_back_past_the_start_clamps_to_zero() -> None:
    """Skipping back further than the current position lands at the start of the item."""
    ctrl, _queue, timers, play_index = _controller(elapsed_time=5.0, anchor_age=0.0)

    await ctrl.skip(QUEUE_ID, -30)
    await timers.fire()

    assert _restarted_at(play_index) == 0


async def test_pending_skip_dropped_when_the_item_moved_on() -> None:
    """A target calculated for the previous item must not be applied to its successor."""
    ctrl, queue, timers, play_index = _controller()

    await ctrl.skip(QUEUE_ID, -10)
    queue.current_item = QueueItem(
        queue_id=QUEUE_ID, queue_item_id="item2", name="chapter two", duration=DURATION
    )
    await timers.fire()

    play_index.assert_not_awaited()
    # the queue must not be left frozen
    assert ctrl._queue_data[QUEUE_ID].transitioning is False


async def test_play_index_clears_a_pending_skip() -> None:
    """Starting another item drops a target that belonged to the replaced stream."""
    ctrl, _queue, _timers, _play_index = _controller(stub_play_index=False)
    ctrl._load_item = AsyncMock()  # type: ignore[method-assign]
    ctrl._get_next_index = Mock(return_value=None)  # type: ignore[method-assign]
    ctrl.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]

    await ctrl.skip(QUEUE_ID, -10)
    await ctrl.play_index(QUEUE_ID, 0)

    assert ctrl._queue_data[QUEUE_ID].pending_skip is None


@pytest.mark.parametrize("command", ["stop", "pause"])
async def test_stop_and_pause_clear_a_pending_skip(command: str) -> None:
    """Ending playback mid-burst must not leave a target to be applied later."""
    ctrl, _queue, _timers, _play_index = _controller()

    await ctrl.skip(QUEUE_ID, -10)
    await getattr(ctrl, command)(QUEUE_ID)

    assert ctrl._queue_data[QUEUE_ID].pending_skip is None


async def test_pause_mid_burst_resumes_at_the_skipped_to_position() -> None:
    """The optimistic target is what a pause captures, so resume honours the skip."""
    ctrl, queue, _timers, _play_index = _controller(elapsed_time=100.0, anchor_age=0.0)

    await ctrl.skip(QUEUE_ID, -30)
    await ctrl.pause(QUEUE_ID)

    assert queue.resume_pos == 70


async def test_skip_checks_permission_before_deferring() -> None:
    """The deferred restart runs outside the caller's context, so the check cannot wait for it."""
    ctrl, _queue, timers, _play_index = _controller()
    ctrl._check_player_permission = Mock(side_effect=InvalidCommand("nope"))  # type: ignore[method-assign]

    with pytest.raises(InvalidCommand):
        await ctrl.skip(QUEUE_ID, 10)

    assert timers.scheduled == {}


async def test_skip_refused_while_an_announcement_is_playing() -> None:
    """A deferred restart would fight the announcement's own restore."""
    ctrl, _queue, timers, _play_index = _controller(announcing=True)

    with pytest.raises(InvalidCommand, match="announcement"):
        await ctrl.skip(QUEUE_ID, 10)

    assert timers.scheduled == {}


async def test_skip_requires_an_item_with_a_duration() -> None:
    """Radio and not-yet-probed items have no range to skip within."""
    ctrl, _queue, timers, _play_index = _controller(duration=None)

    with pytest.raises(InvalidCommand, match="without duration"):
        await ctrl.skip(QUEUE_ID, 10)

    assert timers.scheduled == {}


async def test_skip_requires_an_active_queue() -> None:
    """An inactive queue has no playback position to move."""
    ctrl, queue, timers, _play_index = _controller()
    queue.active = False

    with pytest.raises(InvalidCommand, match="not active"):
        await ctrl.skip(QUEUE_ID, 10)

    assert timers.scheduled == {}


async def test_previous_restarts_the_track_when_past_the_threshold() -> None:
    """A stale anchor must not make a 6s-in previous jump back to the preceding track."""
    ctrl, queue, _timers, _play_index = _controller(elapsed_time=4.5, anchor_age=2.0)
    queue.current_index = 1
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.get_item = Mock(return_value=queue.current_item)  # type: ignore[method-assign]

    await ctrl.previous(QUEUE_ID)

    # corrected position is 6.5s, so the current track restarts rather than stepping back
    assert queue.current_index == 1
