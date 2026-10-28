"""Tests for the sleep timer that stops a queue at the end of its item or chapter."""

from __future__ import annotations

import inspect
import time
from contextlib import nullcontext
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState, RepeatMode, SleepTimerMode
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Audiobook, MediaItemChapter
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.helpers import (
    CompareState,
    get_sleep_timer_stop_position,
)
from music_assistant.controllers.player_queues.state import PlayerQueueData, SleepTimerTarget

BOOK_CHAPTERS = [
    MediaItemChapter(position=1, name="One", start=0, end=100),
    MediaItemChapter(position=2, name="Two", start=100),
    MediaItemChapter(position=3, name="Three", start=250),
]


def _book_item(
    queue_item_id: str = "item_1",
    chapters: list[MediaItemChapter] | None = BOOK_CHAPTERS,
    duration: int | None = 600,
) -> QueueItem:
    """Build a queue item for an audiobook with the given chapters."""
    book = Audiobook(item_id="book_1", provider="library", name="Book 1", provider_mappings=set())
    book.metadata.chapters = chapters
    return QueueItem(
        queue_id="q1",
        queue_item_id=queue_item_id,
        name="Book 1",
        duration=duration,
        media_item=book,
    )


def _controller(
    repeat_mode: RepeatMode = RepeatMode.OFF,
) -> tuple[PlayerQueuesController, PlayerQueue, MagicMock]:
    """Build a bare controller whose queue "q1" plays the first of two books at 40 seconds."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    mass = MagicMock()
    ctrl.mass = mass
    ctrl.logger = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._enqueue_next_item = Mock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=2)
    queue.repeat_mode = repeat_mode
    queue.state = PlaybackState.PLAYING
    queue.current_index = 0
    queue.index_in_buffer = 0
    queue.elapsed_time = 40
    queue.elapsed_time_last_updated = time.time()
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = [_book_item("item_1"), _book_item("item_2", chapters=None)]
    queue_data.served_item_ids = {"item_1", "item_2"}
    queue.current_item = queue_data.items[0]
    ctrl._queue_data = {"q1": queue_data}
    return ctrl, queue, mass


def _states(
    state: PlaybackState,
    current_item_id: str | None,
    prev_item_id: str = "item_1",
    prev_elapsed: int = 0,
) -> tuple[CompareState, CompareState]:
    """Build the state pair of a queue update: what it played before and what it reports now."""
    prev_state: dict[str, Any] = {
        "state": PlaybackState.PLAYING,
        "current_item_id": prev_item_id,
        "current_item": None,
        "last_playing_elapsed_time": prev_elapsed,
    }
    new_state: dict[str, Any] = {
        "state": state,
        "current_item_id": current_item_id,
        "current_item": None,
        "last_playing_elapsed_time": 0,
    }
    return cast("CompareState", prev_state), cast("CompareState", new_state)


def _scheduled(mass: MagicMock) -> list[str]:
    """Return the names of the coroutines the controller scheduled, closing them."""
    names = []
    for scheduled in mass.create_task.call_args_list:
        target = scheduled.args[0]
        if inspect.iscoroutine(target):
            names.append(target.__name__)
            target.close()
    return names


@pytest.mark.parametrize(
    ("mode", "chapters", "duration", "elapsed_time", "expected"),
    [
        (SleepTimerMode.END_OF_ITEM, BOOK_CHAPTERS, 600, 50, 600),
        (SleepTimerMode.END_OF_CHAPTER, BOOK_CHAPTERS, 600, 50, 100),
        # a chapter without an end runs to the next chapter, the last one to the item end
        (SleepTimerMode.END_OF_CHAPTER, BOOK_CHAPTERS, 600, 100, 250),
        (SleepTimerMode.END_OF_CHAPTER, BOOK_CHAPTERS, 600, 300, 600),
        # a seek to the chapter starting at 100.5 lands on the whole second before it
        (
            SleepTimerMode.END_OF_CHAPTER,
            [
                MediaItemChapter(position=1, name="One", start=0, end=100.5),
                MediaItemChapter(position=2, name="Two", start=100.5, end=200),
            ],
            600,
            100,
            200,
        ),
        (SleepTimerMode.END_OF_CHAPTER, None, 600, 50, 600),
        (SleepTimerMode.END_OF_CHAPTER, BOOK_CHAPTERS, None, 50, None),
    ],
)
def test_sleep_timer_stop_position(
    mode: SleepTimerMode,
    chapters: list[MediaItemChapter] | None,
    duration: int | None,
    elapsed_time: float,
    expected: float | None,
) -> None:
    """The stop position is the end of the chapter or item playing at the position."""
    queue_item = _book_item(chapters=chapters, duration=duration)
    assert get_sleep_timer_stop_position(mode, queue_item, elapsed_time) == expected


def test_set_sleep_timer_target_pins_the_end_of_the_chapter() -> None:
    """The target is the end of the chapter playing now, the estimate counts down to it."""
    ctrl, queue, _ = _controller()

    expires_at = ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_CHAPTER)

    assert ctrl._queue_data["q1"].sleep_timer_target == SleepTimerTarget(
        SleepTimerMode.END_OF_CHAPTER, "item_1", 100
    )
    assert expires_at == pytest.approx(time.time() + 60, abs=0.1)
    queue.state = PlaybackState.PAUSED
    assert ctrl._sleep_timer_expires_at(queue) is None


@pytest.mark.parametrize(
    "queue_change",
    [
        {"current_item": None},
        {"current_item": _book_item(duration=None)},
        {"state": PlaybackState.IDLE},
        {"ended": True},
        {"active": False},
    ],
)
def test_set_sleep_timer_target_rejects_an_unusable_queue(queue_change: dict[str, Any]) -> None:
    """The queue must play or hold paused an item with a known end."""
    ctrl, queue, _ = _controller()
    for attribute, value in queue_change.items():
        setattr(queue, attribute, value)

    with pytest.raises(InvalidDataError):
        ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_ITEM)

    assert ctrl._queue_data["q1"].sleep_timer_target is None


@pytest.mark.parametrize(
    ("repeat_mode", "cur_index", "is_skip", "has_target", "expected"),
    [
        (RepeatMode.OFF, 0, False, True, None),
        (RepeatMode.OFF, 0, False, False, 1),
        # a skip still moves on
        (RepeatMode.OFF, 0, True, True, 1),
        (RepeatMode.ONE, 0, False, True, None),
        # another item than the target follows as usual
        (RepeatMode.ALL, 1, False, True, 0),
    ],
)
def test_nothing_follows_the_sleep_timer_target_on_its_own(
    repeat_mode: RepeatMode, cur_index: int, is_skip: bool, has_target: bool, expected: int | None
) -> None:
    """The queue neither advances past nor repeats the item a sleep timer stops at."""
    ctrl, _, _ = _controller(repeat_mode)
    if has_target:
        ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_ITEM)

    assert ctrl._get_next_index("q1", cur_index, is_skip=is_skip) == expected


def test_queue_stops_once_it_reaches_the_sleep_timer_position() -> None:
    """Position reports re-estimate the stop until one reaches it, then the queue stops."""
    ctrl, queue, mass = _controller()
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_CHAPTER)
    queue.elapsed_time = 99
    queue.elapsed_time_last_updated = time.time()

    ctrl._handle_sleep_timer(queue, *_states(PlaybackState.PLAYING, "item_1"))

    assert "_handle_stop" not in _scheduled(mass)
    mass.players.update_sleep_timer_expires_at.assert_called_with(
        "q1", pytest.approx(time.time() + 1, abs=0.1)
    )

    queue.elapsed_time = 100
    queue.elapsed_time_last_updated = time.time()
    ctrl._handle_sleep_timer(queue, *_states(PlaybackState.PLAYING, "item_1"))

    assert "_handle_stop" in _scheduled(mass)
    assert ctrl._queue_data["q1"].sleep_timer_target is None
    mass.players._clear_sleep_timer.assert_called_once_with(mass.players.get_player.return_value)
    # the next item is back, so the stop does not read as the end of the queue
    assert queue.next_item is not None
    assert queue.next_item.queue_item_id == "item_2"


def test_paused_queue_has_no_sleep_timer_estimate() -> None:
    """A paused queue keeps its target and has no estimate of the stop until it plays again."""
    ctrl, queue, mass = _controller()
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_CHAPTER)
    queue.state = PlaybackState.PAUSED

    ctrl._handle_sleep_timer(queue, *_states(PlaybackState.PAUSED, "item_1"))

    mass.players.update_sleep_timer_expires_at.assert_called_with("q1", None)
    assert ctrl._queue_data["q1"].sleep_timer_target is not None


@pytest.mark.parametrize(
    ("reported_item", "prev_elapsed", "stops", "target_item"),
    [
        # a player holding its next item advanced on its own at the end
        ("item_2", 598, True, None),
        # a skip on the device
        ("item_2", 300, False, "item_2"),
        # a player holding the item as its own repeat started it over at the end
        ("item_1", 598, True, None),
        ("item_1", 300, False, "item_1"),
    ],
)
def test_leaving_the_target_item_at_its_end_stops_the_queue(
    reported_item: str, prev_elapsed: int, stops: bool, target_item: str | None
) -> None:
    """The target item ended when the player left it, or started it over, near the stop."""
    ctrl, queue, mass = _controller()
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_ITEM)
    queue.current_index = 0 if reported_item == "item_1" else 1
    queue.current_item = ctrl._queue_data["q1"].items[queue.current_index]
    queue.elapsed_time = 1
    queue.elapsed_time_last_updated = time.time()

    ctrl._handle_sleep_timer(
        queue, *_states(PlaybackState.PLAYING, reported_item, prev_elapsed=prev_elapsed)
    )

    assert ("_handle_stop" in _scheduled(mass)) is stops
    assert ctrl._queue_data["q1"].sleep_timer_target == (
        SleepTimerTarget(SleepTimerMode.END_OF_ITEM, target_item, 600) if target_item else None
    )


def test_a_report_lagging_behind_a_switch_leaves_the_target_alone() -> None:
    """After a switch to the second item, a repeated report of the first one is not a skip."""
    ctrl, queue, mass = _controller()
    ctrl._queue_data["q1"].sleep_timer_target = SleepTimerTarget(
        SleepTimerMode.END_OF_ITEM, "item_2", 600
    )

    ctrl._handle_sleep_timer(queue, *_states(PlaybackState.PLAYING, "item_1", prev_elapsed=598))

    assert "_handle_stop" not in _scheduled(mass)
    assert ctrl._queue_data["q1"].sleep_timer_target == SleepTimerTarget(
        SleepTimerMode.END_OF_ITEM, "item_2", 600
    )


@pytest.mark.parametrize(("active", "stops"), [(True, True), (False, False)])
def test_idle_queue_ends_the_sleep_timer(active: bool, stops: bool) -> None:
    """A queue that stopped ran out of audio at the target; one taken by another source is let go."""
    ctrl, queue, mass = _controller()
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_ITEM)
    queue.active = active
    queue.state = PlaybackState.IDLE
    prev_state, new_state = _states(PlaybackState.IDLE, "item_1")

    ctrl._handle_sleep_timer(queue, prev_state, new_state)
    ctrl._handle_end_of_queue(queue, prev_state, new_state)

    # the stop is not read as the end of the queue, which would settle or resume it
    assert _scheduled(mass) == (["_handle_stop"] if stops else [])
    assert ctrl._queue_data["q1"].sleep_timer_target is None
    mass.players._clear_sleep_timer.assert_called_once_with(mass.players.get_player.return_value)


@pytest.mark.parametrize("stop", ["mark_ended", "_handle_stop"])
async def test_a_stop_and_the_end_of_the_queue_clear_the_sleep_timer(stop: str) -> None:
    """Whatever stops the queue ends what the timer waited for."""
    ctrl, _, mass = _controller()
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_ITEM)
    mass.players.get_group_and_player_lock = Mock(return_value=nullcontext())
    mass.players._handle_cmd_stop = AsyncMock()

    if stop == "mark_ended":
        ctrl.mark_ended("q1")
    else:
        await ctrl._handle_stop("q1")

    _scheduled(mass)
    assert ctrl._queue_data["q1"].sleep_timer_target is None
    mass.players._clear_sleep_timer.assert_called_once_with(mass.players.get_player.return_value)
