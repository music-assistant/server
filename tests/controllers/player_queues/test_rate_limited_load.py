"""Tests that a provider asking to back off stops the queue instead of skipping its items."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    QueueEmpty,
    RateLimited,
    RetriesExhausted,
)
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

from .test_play_request_timeout import _locked, _with_player_locks

QUEUE_ID = "q1"
BACK_OFF_ERRORS = [
    RetriesExhausted("Not retrying, asked to wait 3600 seconds"),
    RateLimited("slow down", backoff_time=3600),
]


def _controller(
    load_error: MusicAssistantError,
) -> tuple[PlayerQueuesController, AsyncMock, AsyncMock]:
    """Build a bare controller whose queue items all fail to load with the given error."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=4)
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = [
        QueueItem(queue_id=QUEUE_ID, queue_item_id=f"item{i}", name=f"item{i}", duration=180)
        for i in range(4)
    ]
    queue.current_index = 0
    queue.current_item = queue_data.items[0]
    ctrl._queue_data = {QUEUE_ID: queue_data}

    def _get_item(_queue_id: str, index: int | str) -> QueueItem | None:
        if isinstance(index, str):
            return next((i for i in queue_data.items if i.queue_item_id == index), None)
        return queue_data.items[index] if 0 <= index < len(queue_data.items) else None

    def _index_by_id(_queue_id: str, item_id: str) -> int | None:
        ids = [item.queue_item_id for item in queue_data.items]
        return ids.index(item_id) if item_id in ids else None

    def _next_index(_queue_id: str, index: int, **_kwargs: bool) -> int | None:
        return index + 1 if index + 1 < len(queue_data.items) else None

    load_mock = AsyncMock(side_effect=load_error)
    stop_mock = AsyncMock()
    ctrl.get = Mock(return_value=queue)  # type: ignore[method-assign]
    ctrl.get_item = Mock(side_effect=_get_item)  # type: ignore[method-assign]
    ctrl.index_by_id = Mock(side_effect=_index_by_id)  # type: ignore[method-assign]
    ctrl._get_next_index = Mock(side_effect=_next_index)  # type: ignore[method-assign]
    ctrl._get_resume_position = AsyncMock(return_value=0)  # type: ignore[method-assign]
    ctrl._load_item = load_mock  # type: ignore[method-assign]
    ctrl._fill_dynamic_tracks = AsyncMock()  # type: ignore[method-assign]
    ctrl.update_items = Mock()  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.stop = stop_mock  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player.return_value.offline_by_power_control = False
    # the queue's player is not in a group
    ctrl.mass.players._resolve_playback_owner = Mock(side_effect=lambda player: player)
    ctrl.logger = MagicMock()
    return ctrl, load_mock, stop_mock


@pytest.mark.parametrize("error", BACK_OFF_ERRORS)
async def test_play_index_stops_on_a_provider_backing_off(error: MusicAssistantError) -> None:
    """Starting an item of a rate limited provider reports that and skips nothing."""
    ctrl, load_mock, stop_mock = _controller(error)

    with pytest.raises(type(error)) as exc_info:
        await ctrl.play_index(QUEUE_ID, 0)

    assert exc_info.value is error
    load_mock.assert_awaited_once()
    assert all(item.available for item in ctrl._queue_data[QUEUE_ID].items)
    stop_mock.assert_awaited_once_with(QUEUE_ID)


@pytest.mark.parametrize("error", BACK_OFF_ERRORS)
async def test_a_refused_start_lets_go_of_its_player_and_the_group_holding_it(
    error: MusicAssistantError,
) -> None:
    """A start is refused while it holds its player and the group above it, and frees both."""
    ctrl, load_mock, stop_mock = _controller(error)
    players = _with_player_locks(ctrl, {QUEUE_ID: "group"})
    # the queue's player plays along with its group
    ctrl.mass.players._resolve_playback_owner = Mock(  # type: ignore[method-assign]
        return_value=Mock()
    )
    end_session = ctrl._end_session = Mock()  # type: ignore[method-assign]
    held_while_loading: list[str] = []

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        held_while_loading.extend(_locked(players, QUEUE_ID, "group"))
        raise error

    load_mock.side_effect = _refuse
    queue_data = ctrl._queue_data[QUEUE_ID]

    with pytest.raises(type(error)) as exc_info:
        await ctrl.play_index(QUEUE_ID, 0)

    assert exc_info.value is error
    assert held_while_loading == [QUEUE_ID, "group"]
    assert _locked(players, QUEUE_ID, "group") == []
    assert not players._task_held_locks
    assert not players._lock_holders
    assert not queue_data.queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS]
    # only the session of this start ends: a stop would end the stream of the group
    stop_mock.assert_not_awaited()
    end_session.assert_called_once_with(queue_data, queue_data.session_id)
    # the next command on the player or on its group gets its lock at once
    async with asyncio.timeout(0), players.get_group_and_player_lock(QUEUE_ID):
        assert _locked(players, QUEUE_ID, "group") == [QUEUE_ID, "group"]


@pytest.mark.parametrize("error", BACK_OFF_ERRORS)
async def test_next_item_load_stops_on_a_provider_backing_off(error: MusicAssistantError) -> None:
    """Loading the next item of a rate limited provider tries no further items."""
    ctrl, load_mock, _ = _controller(error)

    with pytest.raises(type(error)) as exc_info:
        await ctrl.load_next_queue_item(QUEUE_ID, "item0")

    assert exc_info.value is error
    load_mock.assert_awaited_once()
    assert all(item.available for item in ctrl._queue_data[QUEUE_ID].items)


async def test_next_item_load_skips_items_that_are_not_found() -> None:
    """Items a provider does not find are skipped and marked unavailable."""
    ctrl, load_mock, _ = _controller(MediaNotFoundError("gone"))
    items = ctrl._queue_data[QUEUE_ID].items

    with pytest.raises(QueueEmpty):
        await ctrl.load_next_queue_item(QUEUE_ID, "item0")

    assert load_mock.await_count == 3
    assert [item.available for item in items] == [True, False, False, False]
