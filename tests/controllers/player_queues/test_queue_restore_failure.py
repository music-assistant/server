"""
Tests for restoring the queue of a player when it registers.

A cache failure must never leave a player registered without a queue: it falls back to a
fresh queue so the player stays usable. A queue restored for a player that is no longer
live by then is dropped.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues.constants import CACHE_CATEGORY_PLAYER_QUEUE_STATE
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller(cache_get: AsyncMock) -> Any:
    """
    Build a queue-controller stand-in for ``on_player_register``.

    :param cache_get: Stand-in for the cache lookups the queue restore performs.
    """
    queues = MagicMock()
    queues._queue_data = {}
    queues.mass.cache.get = cache_get
    return queues


def _player(queues: Any, player_id: str = "p1") -> Any:
    """
    Build a live player stand-in with the state fields the queue snapshot is built from.

    :param queues: The queue-controller stand-in, whose players controller gets the player.
    :param player_id: The id of the player.
    """
    player = SimpleNamespace(player_id=player_id, name="Player A", available=True)
    queues.mass.players.get_player = MagicMock(
        side_effect=lambda queried_id: player if queried_id == player_id else None
    )
    return player


async def test_player_gets_a_queue_when_the_state_lookup_fails() -> None:
    """A failing cache lookup for the stored queue state falls back to a fresh queue."""
    queues = _controller(AsyncMock(side_effect=OSError("database error")))

    await PlayerQueuesController.on_player_register(queues, _player(queues))

    assert queues._queue_data["p1"].queue.queue_id == "p1"
    queues.logger.warning.assert_called_once()


async def test_player_gets_a_queue_when_the_items_lookup_fails() -> None:
    """A failing cache lookup for the stored queue items falls back to a fresh queue."""
    queues = _controller(AsyncMock(side_effect=[{"queue": {}}, OSError("database error")]))

    await PlayerQueuesController.on_player_register(queues, _player(queues))

    assert queues._queue_data["p1"].queue.queue_id == "p1"
    queues.logger.warning.assert_called_once()


@pytest.mark.parametrize("gone", ["removed", "replaced"])
async def test_queue_restored_for_a_player_that_went_meanwhile_is_dropped(gone: str) -> None:
    """A player removed (or created anew) while its queue was restored gets no queue."""
    stored = PlayerQueueData(
        queue=PlayerQueue(
            queue_id="p1", active=False, display_name="Player A", available=True, items=2
        )
    )
    queues = _controller(AsyncMock())
    player = _player(queues)
    newer_player = SimpleNamespace(player_id="p1")

    async def _cache_get(*, category: int, **_kwargs: Any) -> Any:
        if category == CACHE_CATEGORY_PLAYER_QUEUE_STATE:
            return stored.to_cache()
        # the player goes while the restore waits for its items
        queues.mass.players.get_player.side_effect = lambda _queried_id: (
            None if gone == "removed" else newer_player
        )
        return []

    queues.mass.cache.get.side_effect = _cache_get

    await PlayerQueuesController.on_player_register(queues, player)

    assert queues.mass.cache.get.await_count == 2
    assert queues._queue_data == {}
    queues.on_player_update.assert_not_called()
    queues.mass.signal_event.assert_not_called()
