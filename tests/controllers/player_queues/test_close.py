"""Tests for closing the player queues controller at shutdown."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue


async def test_close_continues_after_a_queue_fails_to_stop() -> None:
    """A queue that fails to stop does not keep the others from stopping and being saved."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl.logger = MagicMock()
    ctrl._queue_data = {
        queue_id: PlayerQueueData(
            queue=cast("PlayerQueue", MagicMock(queue_id=queue_id, state=PlaybackState.PLAYING))
        )
        for queue_id in ("unreachable", "reachable")
    }
    ctrl.stop = AsyncMock(side_effect=[PlayerUnavailableError("gone"), None])  # type: ignore[method-assign]
    ctrl._save_queue_to_cache = AsyncMock()  # type: ignore[method-assign]

    await ctrl.close()

    assert [call.args[0] for call in ctrl.stop.await_args_list] == ["unreachable", "reachable"]
    assert [call.args[0] for call in ctrl._save_queue_to_cache.await_args_list] == [
        "unreachable",
        "reachable",
    ]
