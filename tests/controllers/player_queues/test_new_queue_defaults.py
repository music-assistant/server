"""
Tests for the settings a queue starts out with when its player is first registered.

Autoplay and crossfade each follow the global default until a queue pins its own override (via
the player controls' toggle). A queue restored from a legacy cache (no override key) resets to
follow the current global default rather than keeping its last wire value - the agreed behavior
for the global-default rollout.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller(
    cached_state: dict[str, Any] | None = None,
    *,
    global_autoplay: bool = True,
    global_crossfade: bool = False,
) -> Any:
    """
    Build a queue-controller stand-in for ``on_player_register``.

    :param cached_state: A previously persisted queue state to restore, or None for a new queue.
    :param global_autoplay: The stubbed global autoplay_enabled core config value.
    :param global_crossfade: The stubbed global crossfade_enabled core config value.
    """
    queues = MagicMock()
    queues._queue_data = {}
    queues.mass.cache.get = AsyncMock(side_effect=[cached_state, []])
    defaults = {"autoplay_enabled": global_autoplay, "crossfade_enabled": global_crossfade}
    queues.mass.config.get_raw_core_config_value = MagicMock(
        side_effect=lambda _core_module, key, _default: defaults[key]
    )
    # bind the real resolver so on_player_register's call to it exercises the actual logic
    queues._resolve_default_toggles = lambda queue_data: (
        PlayerQueuesController._resolve_default_toggles(queues, queue_data)
    )
    return queues


def _player(player_id: str = "p1") -> Any:
    """Build a player stand-in with the state fields the queue snapshot is built from."""
    return SimpleNamespace(
        player_id=player_id, state=SimpleNamespace(name="Player A", available=True)
    )


async def test_new_queue_follows_the_global_defaults() -> None:
    """A queue created for a newly registered player follows the global autoplay/crossfade defaults."""
    queues = _controller(global_autoplay=True, global_crossfade=False)

    await PlayerQueuesController.on_player_register(queues, _player())

    queue = queues._queue_data["p1"].queue
    assert queue.autoplay_enabled is True
    assert queue.crossfade_enabled is False


async def test_new_queue_follows_a_flipped_global_default() -> None:
    """A new queue reflects whatever the global defaults are currently set to."""
    queues = _controller(global_autoplay=False, global_crossfade=True)

    await PlayerQueuesController.on_player_register(queues, _player())

    queue = queues._queue_data["p1"].queue
    assert queue.autoplay_enabled is False
    assert queue.crossfade_enabled is True


async def test_restored_queue_without_override_follows_the_global_default() -> None:
    """A legacy cache (no pinned override) resets to follow the current global default."""
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
    queues = _controller(stored.to_cache(), global_autoplay=True)

    await PlayerQueuesController.on_player_register(queues, _player())

    # the stale wire value (False) is overwritten by the current global default (True)
    assert queues._queue_data["p1"].queue.autoplay_enabled is True


async def test_restored_queue_with_pinned_override_keeps_it() -> None:
    """A queue that pinned its own Autoplay setting keeps it regardless of the global default."""
    stored = PlayerQueueData(
        queue=PlayerQueue(
            queue_id="p1",
            active=False,
            display_name="Player A",
            available=True,
            items=0,
        ),
        autoplay_override=False,
    )
    queues = _controller(stored.to_cache(), global_autoplay=True)

    await PlayerQueuesController.on_player_register(queues, _player())

    # the pinned override (False) sticks even though the global default is now True
    assert queues._queue_data["p1"].queue.autoplay_enabled is False
