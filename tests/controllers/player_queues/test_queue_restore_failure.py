"""
Tests for player registration when the queue cache cannot be read.

A cache failure must never leave a player registered without a queue: it falls back to a
fresh queue so the player stays usable.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant.controllers.player_queues.controller import PlayerQueuesController


def _controller(cache_get: AsyncMock) -> Any:
    """
    Build a queue-controller stand-in for ``on_player_register``.

    :param cache_get: Stand-in for the cache lookups the queue restore performs.
    """
    queues = MagicMock()
    queues._queue_data = {}
    queues.mass.cache.get = cache_get
    return queues


def _player(player_id: str = "p1") -> Any:
    """Build a player stand-in with the state fields the queue snapshot is built from."""
    return SimpleNamespace(
        player_id=player_id, state=SimpleNamespace(name="Player A", available=True)
    )


async def test_player_gets_a_queue_when_the_state_lookup_fails() -> None:
    """A failing cache lookup for the stored queue state falls back to a fresh queue."""
    queues = _controller(AsyncMock(side_effect=OSError("database error")))

    await PlayerQueuesController.on_player_register(queues, _player())

    assert queues._queue_data["p1"].queue.queue_id == "p1"
    queues.logger.warning.assert_called_once()


async def test_player_gets_a_queue_when_the_items_lookup_fails() -> None:
    """A failing cache lookup for the stored queue items falls back to a fresh queue."""
    queues = _controller(AsyncMock(side_effect=[{"queue": {}}, OSError("database error")]))

    await PlayerQueuesController.on_player_register(queues, _player())

    assert queues._queue_data["p1"].queue.queue_id == "p1"
    queues.logger.warning.assert_called_once()
