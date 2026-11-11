"""Tests for closing the player queues controller at shutdown."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue


def _controller() -> tuple[PlayerQueuesController, AsyncMock]:
    """Build a bare controller holding two playing queues, and its cache save mock."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl.logger = MagicMock()
    ctrl._queue_data = {
        queue_id: PlayerQueueData(
            queue=cast("PlayerQueue", MagicMock(queue_id=queue_id, state=PlaybackState.PLAYING))
        )
        for queue_id in ("unreachable", "reachable")
    }
    save = AsyncMock()
    ctrl._save_queue_to_cache = save  # type: ignore[method-assign]
    return ctrl, save


async def test_close_continues_after_a_queue_fails_to_stop() -> None:
    """A queue that fails to stop does not keep the others from stopping and being saved."""
    ctrl, save = _controller()
    stop = AsyncMock(side_effect=[PlayerUnavailableError("gone"), None])
    ctrl.stop = stop  # type: ignore[method-assign]

    await ctrl.close()

    assert [call.args[0] for call in stop.await_args_list] == ["unreachable", "reachable"]
    assert [call.args[0] for call in save.await_args_list] == [
        "unreachable",
        "reachable",
    ]


async def test_close_does_not_wait_forever_on_a_stop() -> None:
    """A player that never answers the stop does not keep the queues from being saved."""
    ctrl, save = _controller()
    stopped: list[str] = []

    async def stop(queue_id: str) -> None:
        if queue_id == "unreachable":
            await asyncio.Event().wait()
        stopped.append(queue_id)

    ctrl.stop = stop  # type: ignore[method-assign]

    with patch(
        "music_assistant.controllers.player_queues.controller.QUEUE_SHUTDOWN_STOP_TIMEOUT", 0.01
    ):
        await ctrl.close()

    assert stopped == ["reachable"]
    assert save.await_count == 2


async def test_close_cancels_a_running_save_before_the_final_one() -> None:
    """A save that already started can't overwrite the state written at close."""
    ctrl, save = _controller()
    ctrl.stop = AsyncMock()  # type: ignore[method-assign]
    events: list[str] = []
    cast("MagicMock", ctrl.mass).cancel_task.side_effect = lambda task_id: events.append(
        f"cancel {task_id}"
    )
    save.side_effect = lambda queue_id: events.append(f"save {queue_id}")

    await ctrl.close()

    assert events == [
        "cancel save_queue_cache_unreachable",
        "save unreachable",
        "cancel save_queue_cache_reachable",
        "save reachable",
    ]
