"""Tests for the sleep timer that stops a queue at the end of its item or chapter."""

from __future__ import annotations

import inspect
import time
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType, RepeatMode, SleepTimerMode
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Audiobook, MediaItemChapter
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
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
    prev_playback_state: PlaybackState = PlaybackState.PLAYING,
) -> tuple[CompareState, CompareState]:
    """Build the state pair of a queue update: what it played before and what it reports now."""
    prev_state: dict[str, Any] = {
        "state": prev_playback_state,
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


def _player(state: PlaybackState, queue_item_id: str, elapsed: float) -> Any:
    """Return a non-flow player reporting the queue item and position it plays."""
    return SimpleNamespace(
        player_id="q1",
        type=PlayerType.PLAYER,
        active_output_protocol=None,
        current_media=SimpleNamespace(source_id="q1", queue_item_id=queue_item_id, uri=None),
        state=SimpleNamespace(
            name="Q1",
            available=True,
            playback_state=state,
            active_source="q1",
            corrected_elapsed_time=elapsed,
            group_members=[],
        ),
    )


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
    ctrl, queue, mass = _controller()

    expires_at = ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_CHAPTER)

    assert ctrl._queue_data["q1"].sleep_timer_target == SleepTimerTarget(
        SleepTimerMode.END_OF_CHAPTER, "item_1", 100
    )
    assert expires_at == pytest.approx(time.time() + 60, abs=0.1)
    mass.players.update_sleep_timer.assert_called_once_with(
        "q1", SleepTimerMode.END_OF_CHAPTER, expires_at
    )
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
    mass.players.update_sleep_timer.assert_called_with(
        "q1", SleepTimerMode.END_OF_CHAPTER, pytest.approx(time.time() + 1, abs=0.1)
    )

    queue.elapsed_time = 100
    queue.elapsed_time_last_updated = time.time()
    ctrl._handle_sleep_timer(queue, *_states(PlaybackState.PLAYING, "item_1"))

    assert "_handle_stop" in _scheduled(mass)
    assert ctrl._queue_data["q1"].sleep_timer_target is None
    mass.players.reset_sleep_timer.assert_called_once_with(mass.players.get_player.return_value)
    # a flow stream that ran out does not resume the queue with its next item back
    mass.cancel_task.assert_called_once_with("queue_buffer_completed_q1")
    # the next item is back, so the stop does not read as the end of the queue
    assert queue.next_item is not None
    assert queue.next_item.queue_item_id == "item_2"


def test_paused_queue_has_no_sleep_timer_estimate() -> None:
    """A paused queue keeps its target and has no estimate of the stop until it plays again."""
    ctrl, queue, mass = _controller()
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_CHAPTER)
    queue.state = PlaybackState.PAUSED

    ctrl._handle_sleep_timer(queue, *_states(PlaybackState.PAUSED, "item_1"))

    mass.players.update_sleep_timer.assert_called_with("q1", SleepTimerMode.END_OF_CHAPTER, None)
    assert ctrl._queue_data["q1"].sleep_timer_target is not None


@pytest.mark.parametrize(
    ("reported_item", "prev_elapsed", "enqueued_item", "stops", "target_item"),
    [
        # a player holding its next item advanced on its own at the end
        ("item_2", 598, None, True, None),
        # ... or at the real end of audio shorter than the stated duration
        ("item_2", 300, "item_2", True, None),
        # a skip on the device
        ("item_2", 300, None, False, "item_2"),
        # a player holding the item as its own repeat started it over at the end
        ("item_1", 598, None, True, None),
        ("item_1", 300, None, False, "item_1"),
    ],
)
def test_leaving_the_target_item_at_its_end_stops_the_queue(
    reported_item: str,
    prev_elapsed: int,
    enqueued_item: str | None,
    stops: bool,
    target_item: str | None,
) -> None:
    """The target item ended when the player left it, or started it over, near the stop."""
    ctrl, queue, mass = _controller()
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_ITEM)
    ctrl._queue_data["q1"].next_item_id_enqueued = enqueued_item
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


@pytest.mark.parametrize(
    ("active", "prev_playback_state", "stops", "keeps_target"),
    [
        (True, PlaybackState.PLAYING, True, False),
        (False, PlaybackState.PLAYING, False, False),
        # a paused player that went idle after its pause timeout
        (True, PlaybackState.PAUSED, False, True),
    ],
)
def test_idle_queue_ends_the_sleep_timer(
    active: bool, prev_playback_state: PlaybackState, stops: bool, keeps_target: bool
) -> None:
    """A queue that stopped ran out of audio at the target; one taken by another source is let go."""
    ctrl, queue, mass = _controller()
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_CHAPTER)
    queue.active = active
    queue.state = PlaybackState.IDLE
    prev_state, new_state = _states(
        PlaybackState.IDLE, "item_1", prev_elapsed=50, prev_playback_state=prev_playback_state
    )
    prev_state["current_item"] = queue.current_item

    ctrl._handle_sleep_timer(queue, prev_state, new_state)
    ctrl._handle_end_of_queue(queue, prev_state, new_state)

    # the stop is not read as the end of the queue, which would settle or resume it
    assert _scheduled(mass) == (["_handle_stop"] if stops else [])
    assert (ctrl._queue_data["q1"].sleep_timer_target is not None) is keeps_target
    assert mass.players.reset_sleep_timer.called is not keeps_target


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
    mass.players.reset_sleep_timer.assert_called_once_with(mass.players.get_player.return_value)


@pytest.mark.parametrize(
    ("prev_elapsed", "elapsed_time", "stops"),
    [
        # ordinary playback inside the end margin of a short item
        (3, 4, False),
        # a small position correction is not a restart
        (4, 3.5, False),
        # the player started the item over
        (7, 0.5, True),
    ],
)
def test_short_target_only_stops_when_it_starts_over(
    prev_elapsed: int, elapsed_time: float, stops: bool
) -> None:
    """A target shorter than twice the end margin plays on until it ends or starts over."""
    ctrl, queue, mass = _controller()
    short_item = _book_item("item_1", chapters=None, duration=8)
    ctrl._queue_data["q1"].items[0] = short_item
    queue.current_item = short_item
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_ITEM)
    queue.elapsed_time = elapsed_time
    queue.elapsed_time_last_updated = time.time()

    ctrl._handle_sleep_timer(
        queue, *_states(PlaybackState.PLAYING, "item_1", prev_elapsed=prev_elapsed)
    )

    assert ("_handle_stop" in _scheduled(mass)) is stops


async def test_sleep_timer_stop_on_the_last_item_ends_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop on the last item ends the queue, without resuming or refilling it."""
    ctrl, queue, mass = _controller()
    queue_data = ctrl._queue_data["q1"]
    queue_data.items = queue_data.items[:1]
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_ITEM)
    queue.elapsed_time = 600
    queue.elapsed_time_last_updated = time.time()
    ctrl._handle_sleep_timer(queue, *_states(PlaybackState.PLAYING, "item_1"))
    assert _scheduled(mass) == ["_handle_stop"]
    assert queue.next_item is None
    mass.create_task.reset_mock()

    find_dynamic_source = Mock(return_value=MagicMock())
    monkeypatch.setattr(
        "music_assistant.controllers.player_queues.playback_tracker.find_dynamic_source",
        find_dynamic_source,
    )
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    ctrl.mark_ended = Mock()  # type: ignore[method-assign]
    queue.state = PlaybackState.IDLE
    prev_state, new_state = _states(PlaybackState.IDLE, "item_1", prev_elapsed=600)
    prev_state["current_item"] = queue_data.items[0]
    ctrl._handle_end_of_queue(queue, prev_state, new_state)

    settle = mass.create_task.call_args.args[0]
    await settle

    find_dynamic_source.assert_not_called()
    ctrl.play_index.assert_not_called()
    ctrl.mark_ended.assert_called_once_with("q1")
    assert queue_data.sleep_timer_stopped_target is None


@pytest.mark.parametrize("flow_mode", [False, True])
def test_chapter_stop_in_the_last_item_keeps_the_queue_resumable(flow_mode: bool) -> None:
    """A chapter stop inside the only item is a plain stop, the queue does not end."""
    ctrl, queue, mass = _controller()
    queue_data = ctrl._queue_data["q1"]
    queue_data.items = queue_data.items[:1]
    queue.flow_mode = flow_mode
    # a stop leaves the flow entry of the item it cut short unfinished, see
    # test_flow_ends_unfinished_when_its_source_is_released
    queue_data.flow_mode_stream_log = [PlayLogEntry(queue_item_id="item_1")]
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_CHAPTER)
    queue.elapsed_time = 100
    queue.elapsed_time_last_updated = time.time()
    ctrl._handle_sleep_timer(queue, *_states(PlaybackState.PLAYING, "item_1"))
    assert _scheduled(mass) == ["_handle_stop"]
    mass.create_task.reset_mock()

    queue.state = PlaybackState.IDLE
    prev_state, new_state = _states(PlaybackState.IDLE, "item_1", prev_elapsed=100)
    prev_state["current_item"] = queue_data.items[0]
    ctrl._handle_end_of_queue(queue, prev_state, new_state)

    # the item did not play to its end, so the queue is not settled or ended
    assert _scheduled(mass) == []
    assert queue.ended is False
    assert queue_data.sleep_timer_stopped_target is None


def test_chapter_timer_on_an_item_without_chapters_shows_the_item_end() -> None:
    """The player shows the item end, the target keeps the chapter mode for the next item."""
    ctrl, queue, mass = _controller()
    ctrl.set_sleep_timer_target("q1", SleepTimerMode.END_OF_CHAPTER)
    queue.current_index = 1
    queue.current_item = ctrl._queue_data["q1"].items[1]

    # a skip on the device to the second book, which has no chapters
    ctrl._handle_sleep_timer(queue, *_states(PlaybackState.PLAYING, "item_2", prev_elapsed=50))

    assert ctrl._queue_data["q1"].sleep_timer_target == SleepTimerTarget(
        SleepTimerMode.END_OF_CHAPTER, "item_2", 600
    )
    assert mass.players.update_sleep_timer.call_args.args[:2] == (
        "q1",
        SleepTimerMode.END_OF_ITEM,
    )


def test_playing_again_forgets_an_earlier_sleep_timer_stop() -> None:
    """A player that paused instead of going idle on the stop plays on with a normal queue end."""
    ctrl, queue, _ = _controller()
    queue_data = ctrl._queue_data["q1"]
    queue_data.sleep_timer_stopped_target = SleepTimerTarget(
        SleepTimerMode.END_OF_ITEM, "item_1", 600
    )

    ctrl._handle_sleep_timer(
        queue,
        *_states(PlaybackState.PLAYING, "item_1", prev_playback_state=PlaybackState.PAUSED),
    )

    assert queue_data.sleep_timer_stopped_target is None


@pytest.mark.parametrize(
    ("mode", "stop_report", "resumes_at"),
    [
        # the end of the item: playing on starts the next one
        (SleepTimerMode.END_OF_ITEM, ("item_1", 600), ("item_2", 0)),
        # a player holding its next item reaches the real end of shorter audio and moves on
        (SleepTimerMode.END_OF_ITEM, ("item_2", 2), ("item_2", 2)),
        # the end of a chapter inside the item: playing on starts the next chapter
        (SleepTimerMode.END_OF_CHAPTER, ("item_1", 100), ("item_1", 100)),
    ],
)
async def test_resume_after_a_sleep_timer_stop(
    mode: SleepTimerMode, stop_report: tuple[str, float], resumes_at: tuple[str, int]
) -> None:
    """Player reports drive the stop, a resume picks up after what the timer let finish."""
    ctrl, queue, mass = _controller()
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]
    mass.streams.is_smart_fades_active.return_value = False
    queue_data = ctrl._queue_data["q1"]
    queue_data.next_item_id_enqueued = "item_2"
    ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, "item_1", 40))
    ctrl.set_sleep_timer_target("q1", mode)

    stopped_item, stopped_at = stop_report
    ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, "item_1", 95))
    ctrl._update_queue_from_player(_player(PlaybackState.PLAYING, stopped_item, stopped_at))
    assert "_handle_stop" in _scheduled(mass)
    assert queue_data.sleep_timer_target is None
    # what the scheduled _handle_stop parks on the playing queue before the player goes idle
    queue.resume_pos = int(stopped_at)
    ctrl._update_queue_from_player(_player(PlaybackState.IDLE, stopped_item, stopped_at))

    await ctrl.resume("q1")

    assert ctrl.play_index.call_args.args[1:3] == resumes_at
