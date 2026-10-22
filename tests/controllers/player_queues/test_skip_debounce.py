"""Tests for the next/previous debounce on the player queues controller."""

from __future__ import annotations

import asyncio
import time
from types import MethodType
from typing import cast
from unittest.mock import MagicMock, Mock

import pytest
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues import controller as controller_module
from music_assistant.controllers.player_queues.constants import SKIP_DEBOUNCE_DELAY
from music_assistant.controllers.player_queues.helpers import handle_play_action
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.mass import MusicAssistant
from tests.common import bare_player_controller, use_real_create_task

QUEUE_ID = "q1"


def _controller(*, real_scheduling: bool = False) -> tuple[PlayerQueuesController, PlayerQueue]:
    """
    Build a bare controller with a four item queue playing the second item.

    With real_scheduling the mass stand-in runs the real task, timer and player lock code, so
    a test can let a scheduled play_index actually run under the playback lock.
    """
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=4)
    items = [
        QueueItem(queue_id=QUEUE_ID, queue_item_id=f"item{idx}", name=f"track {idx}", duration=200)
        for idx in range(4)
    ]
    queue.current_index = 1
    queue.current_item = items[1]
    queue.elapsed_time = 60.0
    queue.elapsed_time_last_updated = time.time()
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = items
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player = Mock(return_value=None)
    if real_scheduling:
        use_real_create_task(ctrl.mass)
        ctrl.mass._tracked_timers = {}
        for name in ("call_later", "cancel_task", "cancel_timer"):
            setattr(ctrl.mass, name, MethodType(getattr(MusicAssistant, name), ctrl.mass))
        ctrl.mass.players.get_group_and_player_lock = (
            bare_player_controller().get_group_and_player_lock
        )
    else:
        ctrl.mass.players.get_group_and_player_lock = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.get_item = Mock(side_effect=lambda _queue_id, index: items[index])  # type: ignore[method-assign]
    ctrl._get_next_index = Mock(side_effect=lambda _queue_id, index, _is_skip: index + 1)  # type: ignore[method-assign]
    return ctrl, queue


def _call_later(ctrl: PlayerQueuesController) -> MagicMock:
    return cast("MagicMock", ctrl.mass.call_later)


def _scheduled_delays(ctrl: PlayerQueuesController) -> list[float]:
    return [call.args[0] for call in _call_later(ctrl).call_args_list]


async def test_a_lone_next_press_starts_playback_without_delay() -> None:
    """A single press must not pay the debounce delay: that delay only serves rapid presses."""
    ctrl, queue = _controller()

    await ctrl.next(QUEUE_ID)

    assert queue.current_index == 2
    assert _scheduled_delays(ctrl) == [0]
    _delay, target, queue_id, index = _call_later(ctrl).call_args.args
    assert (target, queue_id, index) == (ctrl.play_index, QUEUE_ID, 2)
    assert _call_later(ctrl).call_args.kwargs["task_id"] == f"queue_play_index_{QUEUE_ID}"


async def test_rapid_presses_debounce_the_later_ones() -> None:
    """Presses within the debounce window reschedule playback behind the full delay."""
    ctrl, queue = _controller()

    await ctrl.next(QUEUE_ID)
    await ctrl.previous(QUEUE_ID)
    await ctrl.previous(QUEUE_ID)

    assert queue.current_index == 0
    assert _scheduled_delays(ctrl) == [0, SKIP_DEBOUNCE_DELAY, SKIP_DEBOUNCE_DELAY]
    # every press reuses the same task id so the pending start is replaced, not stacked
    assert {call.kwargs["task_id"] for call in _call_later(ctrl).call_args_list} == {
        f"queue_play_index_{QUEUE_ID}"
    }


async def test_a_press_after_the_window_is_immediate_again() -> None:
    """Once the burst is over, the next lone press is served without delay."""
    ctrl, _queue = _controller()

    await ctrl.next(QUEUE_ID)
    ctrl._queue_data[QUEUE_ID].last_skip_press -= SKIP_DEBOUNCE_DELAY + 0.1
    await ctrl.previous(QUEUE_ID)

    assert _scheduled_delays(ctrl) == [0, 0]


async def test_a_press_replaces_the_running_start_instead_of_waiting_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start holds the playback lock; the next press must cancel it, not queue up behind it."""
    monkeypatch.setattr(controller_module, "SKIP_DEBOUNCE_DELAY", 0.05)
    ctrl, queue = _controller(real_scheduling=True)
    ctrl.mass.loop = asyncio.get_running_loop()
    started: list[int] = []
    finished: list[int] = []

    async def _play_index(_self: PlayerQueuesController, _queue_id: str, index: int) -> None:
        started.append(index)
        # stands in for the player taking its time to start
        await asyncio.sleep(10)
        finished.append(index)

    ctrl.play_index = MethodType(handle_play_action(_play_index), ctrl)  # type: ignore[method-assign]

    await ctrl.next(QUEUE_ID)
    await asyncio.sleep(0.01)
    assert started == [2]
    # the second press returns at once and the pending start replaces the running one
    await asyncio.wait_for(ctrl.next(QUEUE_ID), 1)
    assert queue.current_index == 3
    await asyncio.sleep(0.2)

    assert started == [2, 3]
    assert finished == []


async def test_a_cancelled_start_leaves_the_queue_on_the_pressed_item() -> None:
    """The player still reports the old item while a start is replaced; it must not win."""
    ctrl, _queue = _controller()
    queue_data = ctrl._queue_data[QUEUE_ID]
    # the real flag handling is under test here
    del ctrl._set_transitioning
    ctrl.mass.players.get_player = Mock(return_value=MagicMock())  # type: ignore[method-assign]

    async def _slow_load(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(10)

    ctrl._load_item = _slow_load  # type: ignore[method-assign]
    ctrl._set_transitioning(QUEUE_ID, True)

    start = asyncio.create_task(ctrl.play_index(QUEUE_ID, 2))
    await asyncio.sleep(0.01)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start

    assert queue_data.transitioning is True
