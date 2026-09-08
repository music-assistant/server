"""Regression tests for next-track recovery when the player goes idle before handoff."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, RepeatMode
from music_assistant_models.errors import AudioError
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

if TYPE_CHECKING:
    from music_assistant.controllers.player_queues.helpers import CompareState

QUEUE_ID = "q1"


class ControllerKwargs(TypedDict, total=False):
    """Typed keyword arguments accepted by the test controller builder."""

    session_id: str | None
    flow_mode: bool
    queue_available: bool
    queue_active: bool
    queue_state: PlaybackState
    player_state: PlaybackState
    player_source: str | None
    current_item: QueueItem | None
    next_item: QueueItem | None
    items: list[QueueItem] | None
    lock: _GateLock | None
    mock_play_index: bool


class _GateLock:
    """Async lock stub that lets a test hold recovery until it mutates state."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        self._depth = 0

    async def __aenter__(self) -> None:
        self.entered.set()
        await self.release.wait()
        current_task = asyncio.current_task()
        if self._owner is current_task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = current_task
        self._depth = 1

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._owner is not asyncio.current_task():
            return
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


@dataclass(slots=True)
class ControllerHarness:
    """Objects owned by a bare player queues controller test harness."""

    ctrl: PlayerQueuesController
    queue_data: PlayerQueueData
    current_item: QueueItem
    next_item: QueueItem
    tasks: list[asyncio.Task[Any]]
    player: Any
    play_index_mock: AsyncMock | None
    load_item_mock: AsyncMock
    play_media_mock: AsyncMock
    start_session_mock: Mock
    pause_command_mock: AsyncMock
    stop_command_mock: AsyncMock


def _queue_item(
    item_id: str,
    *,
    duration: int | None = 100,
    media_type: MediaType = MediaType.TRACK,
    available: bool = True,
) -> QueueItem:
    """Build a minimal queue item for end-of-track recovery tests."""
    item = QueueItem(
        queue_id=QUEUE_ID,
        queue_item_id=item_id,
        name=item_id,
        duration=duration,
        available=available,
    )
    if media_type != MediaType.TRACK:
        item.media_item = SimpleNamespace(media_type=media_type)  # type: ignore[assignment]
    return item


def _states(
    current_item: QueueItem,
    *,
    prev_state: PlaybackState = PlaybackState.PLAYING,
    seconds_played: int = 98,
) -> tuple[CompareState, CompareState]:
    """Build the state transition into idle for a queue item that nearly finished."""
    return cast(
        "CompareState",
        {
            "state": prev_state,
            "current_item_id": current_item.queue_item_id,
            "current_item": current_item,
            "last_playing_elapsed_time": seconds_played,
        },
    ), cast("CompareState", {"state": PlaybackState.IDLE})


def _controller(  # noqa: PLR0913, PLR0915
    *,
    session_id: str | None = "sess-1",
    flow_mode: bool = False,
    queue_available: bool = True,
    queue_active: bool = True,
    queue_state: PlaybackState = PlaybackState.IDLE,
    player_state: PlaybackState = PlaybackState.IDLE,
    player_source: str | None = QUEUE_ID,
    current_item: QueueItem | None = None,
    next_item: QueueItem | None = None,
    items: list[QueueItem] | None = None,
    lock: _GateLock | None = None,
    mock_play_index: bool = True,
) -> ControllerHarness:
    """Build a bare controller with a queue that just finished one item and has another queued."""
    if items is None:
        current_item = current_item or _queue_item("current")
        next_item = next_item or _queue_item("next")
        items = [current_item, next_item]
    else:
        current_item = current_item or items[0]
        next_item = next_item or items[1]
    queue = PlayerQueue(
        queue_id=QUEUE_ID,
        active=queue_active,
        display_name="Queue",
        available=queue_available,
        items=len(items),
        state=queue_state,
        current_index=0,
        current_item=current_item,
        next_item=next_item,
        flow_mode=flow_mode,
    )
    queue_data = PlayerQueueData(queue=queue, items=items, session_id=session_id)
    player = SimpleNamespace(
        state=SimpleNamespace(
            playback_state=player_state,
            active_source=player_source,
            powered=True,
            name="Player",
        ),
        extra_data={},
        play=AsyncMock(),
    )
    created_tasks: list[asyncio.Task[Any]] = []
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl._queue_data = {QUEUE_ID: queue_data}
    ctrl.logger = MagicMock()
    ctrl.mass = MagicMock()
    ctrl.mass.cancel_timer = Mock()
    ctrl.mass.cancel_task = Mock()
    ctrl.mass.call_later = Mock()
    ctrl.mass.signal_event = Mock()
    ctrl.mass.music.get_playback_speed = AsyncMock(return_value=1.0)
    ctrl.mass.players.get_player = Mock(return_value=player)
    ctrl.mass.players.trigger_player_update = Mock()
    ctrl.mass.streams.audio_processing.clear = Mock()
    ctrl.mass.streams.audio_processing.prune = Mock()
    load_item_mock = AsyncMock()
    play_media_mock = AsyncMock()
    start_session_mock = Mock()
    pause_command_mock = AsyncMock()
    stop_command_mock = AsyncMock()
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl._load_item = load_item_mock  # type: ignore[method-assign]
    ctrl._cleanup_queue_audio_data = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass.players.play_media = play_media_mock
    ctrl.mass.streams.audio_processing.start_session = start_session_mock
    ctrl.mass.players._handle_cmd_pause = pause_command_mock
    ctrl.mass.players._handle_cmd_stop = stop_command_mock
    play_index_mock: AsyncMock | None = None
    if mock_play_index:
        play_index_mock = AsyncMock()
        ctrl.play_index = play_index_mock  # type: ignore[method-assign]

    def _create_task(coro: Any, **_kwargs: object) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        created_tasks.append(task)
        return task

    ctrl.mass.create_task = Mock(side_effect=_create_task)
    if lock is None:

        @asynccontextmanager
        async def _no_lock(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
            yield

        ctrl.mass.players.get_player_lock = Mock(side_effect=_no_lock)
    else:
        ctrl.mass.players.get_player_lock = Mock(return_value=lock)

    @asynccontextmanager
    async def _player_update_waiter(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    ctrl.mass.players.wait_for_player_update = Mock(side_effect=_player_update_waiter)
    return ControllerHarness(
        ctrl=ctrl,
        queue_data=queue_data,
        current_item=current_item,
        next_item=next_item,
        tasks=created_tasks,
        player=player,
        play_index_mock=play_index_mock,
        load_item_mock=load_item_mock,
        play_media_mock=play_media_mock,
        start_session_mock=start_session_mock,
        pause_command_mock=pause_command_mock,
        stop_command_mock=stop_command_mock,
    )


async def _drain(tasks: list[asyncio.Task[Any]]) -> None:
    """Await and clear the tasks created by a test controller."""
    while tasks:
        pending_tasks = tasks.copy()
        tasks.clear()
        await asyncio.gather(*pending_tasks)


def _last_recovered_finished_item_id(queue_data: PlayerQueueData) -> str | None:
    """Return the queue item id most recently used for natural-end recovery."""
    return queue_data.last_recovered_finished_item_id


def _recovery_suppressed_session_id(queue_data: PlayerQueueData) -> str | None:
    """Return the session id currently suppressing natural-end recovery."""
    return queue_data.end_of_track_recovery_suppressed_session_id


_NON_NATURAL_CASES: tuple[tuple[ControllerKwargs, PlaybackState, int], ...] = (
    ({"session_id": None}, PlaybackState.PLAYING, 98),
    ({}, PlaybackState.IDLE, 98),
    ({}, PlaybackState.PAUSED, 98),
    ({}, PlaybackState.PLAYING, 80),
    ({"current_item": _queue_item("current", duration=None)}, PlaybackState.PLAYING, 98),
    (
        {"current_item": _queue_item("current", media_type=MediaType.AUDIO_SOURCE)},
        PlaybackState.PLAYING,
        98,
    ),
    ({"flow_mode": True}, PlaybackState.PLAYING, 98),
    ({"queue_active": False}, PlaybackState.PLAYING, 98),
    ({"queue_available": False}, PlaybackState.PLAYING, 98),
)


@pytest.mark.parametrize(("kwargs", "prev_state", "seconds_played"), _NON_NATURAL_CASES)
async def test_non_natural_end_states_do_not_schedule_recovery(
    kwargs: ControllerKwargs, prev_state: PlaybackState, seconds_played: int
) -> None:
    """Only a finite track ending naturally on an active queue schedules recovery."""
    harness = _controller(**kwargs)
    prev_queue_state, new_queue_state = _states(
        harness.current_item, prev_state=prev_state, seconds_played=seconds_played
    )

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    assert harness.tasks == []


async def test_finished_track_before_handoff_recovers_the_next_item_by_id() -> None:
    """A natural end before enqueue-next lands starts the queued next item once."""
    harness = _controller()
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    assert len(harness.tasks) == 1
    await _drain(harness.tasks)

    harness.play_index_mock.assert_awaited_once_with(QUEUE_ID, harness.next_item.queue_item_id)
    assert harness.queue_data.end_of_track_recovery_key == (
        "sess-1",
        harness.current_item.queue_item_id,
    )
    assert harness.queue_data.last_recovered_finished_item_id == harness.current_item.queue_item_id


async def test_recovery_recomputes_the_next_item_after_queue_edits() -> None:
    """Recovery revalidates the queue under lock and still starts the same next item."""
    gate_lock = _GateLock()
    harness = _controller(lock=gate_lock)
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    await gate_lock.entered.wait()
    harness.queue_data.items.insert(1, _queue_item("skip", available=False))
    harness.queue_data.queue.items = len(harness.queue_data.items)
    gate_lock.release.set()
    await _drain(harness.tasks)

    harness.play_index_mock.assert_awaited_once_with(QUEUE_ID, harness.next_item.queue_item_id)


async def test_recovery_revalidates_the_session_after_waiting_for_the_lock() -> None:
    """A queued recovery may not touch a newer playback session."""
    gate_lock = _GateLock()
    harness = _controller(lock=gate_lock)
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    await gate_lock.entered.wait()
    harness.queue_data.session_id = "sess-2"
    gate_lock.release.set()
    await _drain(harness.tasks)

    harness.play_index_mock.assert_not_awaited()


async def test_recovery_does_not_touch_replaced_queue_state() -> None:
    """A queued recovery is discarded when the queue record was replaced."""
    gate_lock = _GateLock()
    harness = _controller(lock=gate_lock)
    assert harness.play_index_mock is not None
    replacement = PlayerQueueData(
        queue=harness.queue_data.queue,
        items=[harness.current_item, harness.next_item],
        session_id=harness.queue_data.session_id,
    )
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    await gate_lock.entered.wait()
    harness.ctrl._queue_data[QUEUE_ID] = replacement
    gate_lock.release.set()
    await _drain(harness.tasks)

    harness.play_index_mock.assert_not_awaited()
    assert replacement.end_of_track_recovery_key is None
    assert replacement.last_recovered_finished_item_id is None


async def test_recovery_does_not_touch_a_queue_that_already_moved_on() -> None:
    """A moved playhead means the queued recovery is stale and must do nothing."""
    gate_lock = _GateLock()
    harness = _controller(lock=gate_lock)
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    await gate_lock.entered.wait()
    harness.queue_data.queue.current_index = 1
    harness.queue_data.queue.current_item = harness.next_item
    harness.queue_data.queue.next_item = None
    gate_lock.release.set()
    await _drain(harness.tasks)

    harness.play_index_mock.assert_not_awaited()


@pytest.mark.parametrize("stale_field", ["play_action_refcount", "transitioning"])
async def test_recovery_rechecks_play_action_state_after_waiting_for_the_lock(
    stale_field: Literal["play_action_refcount", "transitioning"],
) -> None:
    """Recovery yields to user playback actions that started while it waited."""
    gate_lock = _GateLock()
    harness = _controller(lock=gate_lock)
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    await gate_lock.entered.wait()
    if stale_field == "play_action_refcount":
        harness.queue_data.play_action_refcount = 1
    else:
        harness.queue_data.transitioning = True
    gate_lock.release.set()
    await _drain(harness.tasks)

    harness.play_index_mock.assert_not_awaited()


@pytest.mark.parametrize(
    ("player_state", "player_source"),
    [
        (PlaybackState.PLAYING, QUEUE_ID),
        (PlaybackState.IDLE, "other-source"),
    ],
)
async def test_recovery_aborts_when_the_player_is_no_longer_idle_on_the_queue(
    player_state: PlaybackState, player_source: str
) -> None:
    """Recovery yields to native playback changes that already resumed or switched sources."""
    harness = _controller(
        player_state=player_state,
        player_source=player_source,
    )
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    await _drain(harness.tasks)

    harness.play_index_mock.assert_not_awaited()


async def test_pause_fallback_stop_suppresses_recovery_before_pause_handler() -> None:
    """A user pause that falls back to stop must not look like a natural end."""
    harness = _controller(queue_state=PlaybackState.PLAYING, player_state=PlaybackState.PLAYING)
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)

    async def _fallback_stop(_queue_id: str) -> None:
        assert harness.queue_data.end_of_track_recovery_suppressed_session_id == "sess-1"
        harness.player.state.playback_state = PlaybackState.IDLE
        harness.ctrl._handle_end_of_queue(
            harness.queue_data.queue, prev_queue_state, new_queue_state
        )

    harness.pause_command_mock.side_effect = _fallback_stop

    await harness.ctrl.pause(QUEUE_ID)
    await _drain(harness.tasks)

    harness.play_index_mock.assert_not_awaited()
    assert _recovery_suppressed_session_id(harness.queue_data) == "sess-1"


async def test_external_power_off_does_not_restart_the_queue() -> None:
    """Recovery must not power a player back on before its deferred stop runs."""
    harness = _controller()
    assert harness.play_index_mock is not None
    harness.player.state.powered = False
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)
    await _drain(harness.tasks)

    harness.play_index_mock.assert_not_awaited()


async def test_pause_while_recovery_waits_suppresses_until_explicit_new_play() -> None:
    """A delayed pause suppresses pending recovery until the user intentionally starts playback."""
    gate_lock = _GateLock()
    harness = _controller(lock=gate_lock, mock_play_index=False)
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    await gate_lock.entered.wait()
    await harness.ctrl.pause(QUEUE_ID)
    gate_lock.release.set()
    await _drain(harness.tasks)

    harness.play_media_mock.assert_not_awaited()
    assert harness.queue_data.end_of_track_recovery_suppressed_session_id == "sess-1"
    assert _last_recovered_finished_item_id(harness.queue_data) is None

    await harness.ctrl.play_index(QUEUE_ID, harness.current_item.queue_item_id)
    assert _recovery_suppressed_session_id(harness.queue_data) is None
    assert _last_recovered_finished_item_id(harness.queue_data) is None
    harness.play_media_mock.reset_mock()

    prev_queue_state, new_queue_state = _states(harness.current_item)
    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)
    await _drain(harness.tasks)

    assert harness.queue_data.queue.current_item == harness.next_item
    harness.play_media_mock.assert_awaited_once()


async def test_unpause_rearms_recovery_cancelled_before_it_started() -> None:
    """Cancelling a pending recovery with pause must not consume the item's recovery attempt."""
    gate_lock = _GateLock()
    harness = _controller(lock=gate_lock)
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)
    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    await gate_lock.entered.wait()
    await harness.ctrl.pause(QUEUE_ID)
    gate_lock.release.set()
    await _drain(harness.tasks)

    harness.queue_data.queue.state = PlaybackState.PAUSED
    harness.player.state.playback_state = PlaybackState.PAUSED
    await harness.ctrl.play(QUEUE_ID)
    harness.player.play.assert_awaited_once()

    harness.queue_data.queue.state = PlaybackState.IDLE
    harness.player.state.playback_state = PlaybackState.IDLE
    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)
    await _drain(harness.tasks)

    harness.play_index_mock.assert_awaited_once_with(QUEUE_ID, harness.next_item.queue_item_id)


async def test_explicit_stop_while_recovery_waits_suppresses_before_stop_handler() -> None:
    """A user stop that reaches the lock after recovery still cancels the pending recovery."""
    gate_lock = _GateLock()
    harness = _controller(lock=gate_lock)
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    await gate_lock.entered.wait()
    stop_task = asyncio.create_task(harness.ctrl.stop(QUEUE_ID))
    await asyncio.sleep(0)
    assert harness.queue_data.end_of_track_recovery_suppressed_session_id == "sess-1"
    gate_lock.release.set()
    await stop_task
    await _drain(harness.tasks)

    harness.play_index_mock.assert_not_awaited()
    harness.stop_command_mock.assert_awaited_once_with(QUEUE_ID)


async def test_failed_real_recovery_is_one_shot_until_explicit_replay() -> None:
    """A failed real recovery does not retry until the user explicitly restarts the item."""
    harness = _controller(mock_play_index=False)
    harness.load_item_mock.side_effect = [AudioError("decoder"), None, None]

    async def _start_new_session_during_stop(_queue_id: str) -> None:
        harness.queue_data.session_id = "sess-after-stop"

    harness.stop_command_mock.side_effect = _start_new_session_during_stop
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)
    await _drain(harness.tasks)

    started_session = harness.start_session_mock.call_args.args[1]
    assert started_session != "sess-1"
    harness.stop_command_mock.assert_awaited_once_with(QUEUE_ID)
    assert harness.queue_data.session_id == "sess-after-stop"
    assert (
        _last_recovered_finished_item_id(harness.queue_data) == harness.current_item.queue_item_id
    )

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)

    assert harness.tasks == []

    await harness.ctrl.play_index(QUEUE_ID, harness.current_item.queue_item_id)
    assert _last_recovered_finished_item_id(harness.queue_data) is None
    harness.play_media_mock.reset_mock()

    prev_queue_state, new_queue_state = _states(harness.current_item)
    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)
    await _drain(harness.tasks)

    assert harness.queue_data.queue.current_item == harness.next_item
    assert harness.load_item_mock.await_count == 3
    harness.play_media_mock.assert_awaited_once()


async def test_a_distinct_later_finished_item_can_still_recover() -> None:
    """One-shot protection stays scoped to the finished item instead of blocking later playback."""
    third_item = _queue_item("third")
    harness = _controller(items=[_queue_item("current"), _queue_item("next"), third_item])
    assert harness.play_index_mock is not None
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)
    await _drain(harness.tasks)
    harness.play_index_mock.reset_mock()

    harness.queue_data.queue.current_index = 1
    harness.queue_data.queue.current_item = harness.next_item
    harness.queue_data.queue.next_item = third_item
    prev_queue_state, new_queue_state = _states(harness.next_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)
    await _drain(harness.tasks)

    harness.play_index_mock.assert_awaited_once_with(QUEUE_ID, third_item.queue_item_id)


async def test_repeat_one_recovery_does_not_rearm_itself_on_a_failed_start() -> None:
    """Repeating the finished item cannot reset its own one-shot recovery guard."""
    harness = _controller(mock_play_index=False)
    harness.queue_data.queue.repeat_mode = RepeatMode.ONE
    harness.queue_data.queue.next_item = harness.current_item
    prev_queue_state, new_queue_state = _states(harness.current_item)

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)
    await _drain(harness.tasks)
    assert harness.queue_data.session_id != "sess-1"
    assert (
        _last_recovered_finished_item_id(harness.queue_data) == harness.current_item.queue_item_id
    )

    harness.ctrl._handle_end_of_queue(harness.queue_data.queue, prev_queue_state, new_queue_state)
    await _drain(harness.tasks)

    harness.play_media_mock.assert_awaited_once()
