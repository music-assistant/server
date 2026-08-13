"""Tests that ``play_index`` tops up a dynamic queue before declaring it exhausted."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "q1"


def _controller(
    *, is_dynamic: bool
) -> tuple[PlayerQueuesController, PlayerQueueData, AsyncMock, AsyncMock]:
    """Build a bare controller whose only queue item fails to load."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q1", available=True, items=1)
    queue.is_dynamic = is_dynamic
    queue.current_index = 0
    queue_data = PlayerQueueData(queue=queue)
    queue_data.items = [
        QueueItem(queue_id=QUEUE_ID, queue_item_id="dead", name="dead", duration=180)
    ]
    queue.current_item = queue_data.items[0]
    ctrl._queue_data = {QUEUE_ID: queue_data}

    def _get_item(_queue_id: str, index: int | str) -> QueueItem | None:
        """Return the item at the given index, mirroring the real bounds check."""
        if isinstance(index, int) and 0 <= index < len(queue_data.items):
            return queue_data.items[index]
        return None

    def _next_index(_queue_id: str, index: int, allow_repeat: bool = True) -> int | None:  # noqa: ARG001
        """Report the next index against the live item list, so a refill changes the answer."""
        return index + 1 if index + 1 < len(queue_data.items) else None

    async def _fill(_queue_id: str) -> None:
        """Stand in for the managed-pool top-up by appending one playable item."""
        queue_data.items.append(
            QueueItem(queue_id=QUEUE_ID, queue_item_id="fresh", name="fresh", duration=180)
        )

    # the first item is unplayable (an expired stream url); anything appended later loads fine
    async def _load(queue_item: QueueItem, *_args: object, **_kwargs: object) -> None:
        if queue_item.queue_item_id == "dead":
            raise MediaNotFoundError("expired")

    fill_mock = AsyncMock(side_effect=_fill)
    stop_mock = AsyncMock()
    ctrl.get_item = Mock(side_effect=_get_item)  # type: ignore[method-assign]
    ctrl._get_next_index = Mock(side_effect=_next_index)  # type: ignore[method-assign]
    ctrl._fill_dynamic_tracks = fill_mock  # type: ignore[method-assign]
    ctrl._load_item = AsyncMock(side_effect=_load)  # type: ignore[method-assign]
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl._check_player_permission = Mock()  # type: ignore[method-assign]
    ctrl._set_transitioning = Mock()  # type: ignore[method-assign]
    ctrl.stop = stop_mock  # type: ignore[method-assign]
    ctrl.player_media_from_queue_item = AsyncMock()  # type: ignore[method-assign]
    ctrl.mass = MagicMock()
    ctrl.mass.players.play_media = AsyncMock()
    ctrl.logger = MagicMock()
    return ctrl, queue_data, fill_mock, stop_mock


def _controller_with_dead_head(
    *, dead: int, pre_marked: bool
) -> tuple[PlayerQueuesController, PlayerQueueData, AsyncMock]:
    """Build a controller whose queue starts with `dead` unplayable items."""
    ctrl, queue_data, fill_mock, _stop_mock = _controller(is_dynamic=True)
    queue_data.items = [
        QueueItem(queue_id=QUEUE_ID, queue_item_id=f"dead{i}", name=f"dead{i}", duration=180)
        for i in range(dead)
    ]
    if pre_marked:
        for item in queue_data.items:
            item.available = False
    queue_data.queue.current_index = 0
    queue_data.queue.current_item = queue_data.items[0]

    async def _fill(_queue_id: str) -> None:
        """Append one playable item, as a real dynamic refill would."""
        queue_data.items.append(
            QueueItem(queue_id=QUEUE_ID, queue_item_id="live", name="live", duration=180)
        )

    fill_mock.side_effect = _fill

    async def _load(item: QueueItem, *args: object, **kwargs: object) -> None:  # noqa: ARG001
        """Fail for every dead item, succeed for the refilled one."""
        if item.queue_item_id.startswith("dead"):
            raise MediaNotFoundError(item.queue_item_id)

    ctrl._load_item.side_effect = _load  # type: ignore[attr-defined]
    return ctrl, queue_data, fill_mock


async def test_refilled_track_is_reached_past_a_full_budget_of_dead_items() -> None:
    """Five dead items must not consume the budget the refilled track needs."""
    ctrl, queue_data, fill_mock = _controller_with_dead_head(dead=5, pre_marked=False)
    await ctrl.play_index(QUEUE_ID, 0)
    fill_mock.assert_awaited()
    assert queue_data.queue.current_item is not None
    assert queue_data.queue.current_item.queue_item_id == "live"


async def test_known_dead_items_do_not_spend_the_retry_budget() -> None:
    """A second press must skip already-dead items for free and reach the live one."""
    ctrl, queue_data, _fill = _controller_with_dead_head(dead=5, pre_marked=True)
    queue_data.items.append(
        QueueItem(queue_id=QUEUE_ID, queue_item_id="live", name="live", duration=180)
    )
    await ctrl.play_index(QUEUE_ID, 0)
    assert queue_data.queue.current_item is not None
    assert queue_data.queue.current_item.queue_item_id == "live"


async def test_a_refill_that_yields_nothing_still_fails() -> None:
    """The fix must not turn an genuinely exhausted queue into an infinite loop."""
    ctrl, _queue_data, fill_mock = _controller_with_dead_head(dead=5, pre_marked=False)
    fill_mock.side_effect = None  # a refill that adds nothing
    with pytest.raises(MediaNotFoundError):
        await ctrl.play_index(QUEUE_ID, 0)


async def test_a_non_dynamic_queue_is_unchanged() -> None:
    """Without a dynamic source there is no refill, and the queue still fails as before."""
    ctrl, queue_data, fill_mock = _controller_with_dead_head(dead=5, pre_marked=False)
    queue_data.queue.is_dynamic = False
    with pytest.raises(MediaNotFoundError):
        await ctrl.play_index(QUEUE_ID, 0)
    fill_mock.assert_not_awaited()


async def test_dynamic_queue_refills_instead_of_reporting_exhaustion() -> None:
    """
    An unplayable last item must trigger a top-up, not "no more tracks available".

    _handle_end_of_queue only refills on a playing/paused -> idle transition, so a queue that
    fails from idle never reaches it. Without this the queue stays stuck until the user acts.
    """
    ctrl, queue_data, fill_mock, stop_mock = _controller(is_dynamic=True)

    await ctrl.play_index(QUEUE_ID, 0)

    fill_mock.assert_awaited_once_with(QUEUE_ID)
    assert queue_data.queue.current_item is not None
    assert queue_data.queue.current_item.queue_item_id == "fresh"
    stop_mock.assert_not_awaited()


async def test_non_dynamic_queue_still_reports_exhaustion() -> None:
    """A finite queue has nothing to top up from, so it must fail as before."""
    ctrl, _queue_data, fill_mock, stop_mock = _controller(is_dynamic=False)

    with pytest.raises(MediaNotFoundError):
        await ctrl.play_index(QUEUE_ID, 0)

    fill_mock.assert_not_awaited()
    stop_mock.assert_awaited_once()
