"""
Tests for the settings a queue starts out with when its player is first registered.

Autoplay is on out of the box, but only for a queue that is genuinely new: a queue restored from
cache keeps the switch the user left it on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller(cached_state: dict[str, Any] | None = None) -> Any:
    """
    Build a queue-controller stand-in for ``on_player_register``.

    :param cached_state: A previously persisted queue state to restore, or None for a new queue.
    """
    queues = MagicMock()
    queues._queue_data = {}
    queues.mass.cache.get = AsyncMock(side_effect=[cached_state, []])
    return queues


def _player(player_id: str = "p1") -> Any:
    """Build a player stand-in with the state fields the queue snapshot is built from."""
    return SimpleNamespace(
        player_id=player_id, state=SimpleNamespace(name="Player A", available=True)
    )


async def test_new_queue_starts_with_autoplay_on() -> None:
    """A queue created for a newly registered player starts out with Autoplay on."""
    queues = _controller()

    await PlayerQueuesController.on_player_register(queues, _player())

    assert queues._queue_data["p1"].queue.autoplay_enabled is True


async def test_restored_queue_keeps_its_own_autoplay_setting() -> None:
    """An existing queue is restored as-is: switching Autoplay off has to stick across a restart."""
    stored = PlayerQueueData(
        queue=PlayerQueue(
            queue_id="p1",
            active=False,
            display_name="Player A",
            available=True,
            autoplay_enabled=False,
            items=0,
        )
    )
    queues = _controller(stored.to_cache())

    await PlayerQueuesController.on_player_register(queues, _player())

    assert queues._queue_data["p1"].queue.autoplay_enabled is False
