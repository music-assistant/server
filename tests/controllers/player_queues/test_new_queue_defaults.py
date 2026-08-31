"""
Tests for the settings a queue starts out with when its player is first registered.

A brand new queue follows the global crossfade/autoplay default; a queue restored from cache keeps
whatever the user pinned from the player controls, and a snapshot written before overrides existed
is pinned only where its stored value actually diverges from the current global default.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import CONF_VALUE_DISABLED, CONF_VALUE_ENABLED
from music_assistant.controllers.player_queues.constants import (
    CACHE_FORMAT_VERSION,
    CONF_AUTOPLAY_ENABLED,
    CONF_CROSSFADE_ENABLED,
)
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller(cached_state: dict[str, Any] | None = None, /, **global_defaults: str) -> Any:
    """
    Build a queue-controller stand-in for ``on_player_register``.

    :param cached_state: A previously persisted queue state to restore, or None for a new queue.
    :param global_defaults: The value ``get_raw_core_config_value`` should return for a given
        config key (e.g. ``crossfade_enabled="enabled"``); a key not given falls back to the
        default value the caller passed in.
    """
    queues = MagicMock()
    queues._queue_data = {}
    queues.mass.cache.get = AsyncMock(side_effect=[cached_state, []])
    queues.mass.config.get_raw_core_config_value = MagicMock(
        side_effect=lambda _domain, key, default: global_defaults.get(key, default)
    )
    # bind the real toggle-resolution helpers so on_player_register exercises the actual
    # global-default/override logic instead of a no-op mock
    for name in ("_global_toggle_default", "_apply_toggle_state", "_import_legacy_toggles"):
        setattr(queues, name, MethodType(getattr(PlayerQueuesController, name), queues))
    return queues


def _player(player_id: str = "p1") -> Any:
    """Build a player stand-in with the state fields the queue snapshot is built from."""
    return SimpleNamespace(
        player_id=player_id, state=SimpleNamespace(name="Player A", available=True)
    )


def _legacy_state(*, crossfade_enabled: bool) -> dict[str, Any]:
    """Build a pre-override cache snapshot: wire `crossfade_enabled`, no override fields."""
    return {
        "cache_format_version": CACHE_FORMAT_VERSION,
        "queue": {
            "queue_id": "p1",
            "active": False,
            "display_name": "Player A",
            "available": True,
            "items": 0,
            "crossfade_enabled": crossfade_enabled,
        },
    }


async def test_new_queue_starts_with_autoplay_on() -> None:
    """A queue created for a newly registered player follows the global Autoplay default."""
    queues = _controller()

    await PlayerQueuesController.on_player_register(queues, _player())

    assert queues._queue_data["p1"].queue.autoplay_enabled is True


async def test_restored_queue_keeps_its_own_autoplay_setting() -> None:
    """A restored queue's pinned Autoplay override survives, whatever the global default is."""
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
    # the global default is "on"; the pinned override still wins
    queues = _controller(stored.to_cache(), autoplay_enabled=CONF_VALUE_ENABLED)

    await PlayerQueuesController.on_player_register(queues, _player())

    assert queues._queue_data["p1"].queue.autoplay_enabled is False


@pytest.mark.parametrize(
    ("config_key", "global_value", "expected"),
    [
        (CONF_CROSSFADE_ENABLED, CONF_VALUE_ENABLED, True),
        (CONF_CROSSFADE_ENABLED, CONF_VALUE_DISABLED, False),
        (CONF_AUTOPLAY_ENABLED, CONF_VALUE_ENABLED, True),
        (CONF_AUTOPLAY_ENABLED, CONF_VALUE_DISABLED, False),
    ],
)
async def test_new_queue_follows_the_global_default(
    config_key: str, global_value: str, expected: bool
) -> None:
    """A brand new queue takes whatever the matching global default currently says."""
    queues = _controller(**{config_key: global_value})

    await PlayerQueuesController.on_player_register(queues, _player())

    wire_attr = "crossfade_enabled" if config_key == CONF_CROSSFADE_ENABLED else "autoplay_enabled"
    assert getattr(queues._queue_data["p1"].queue, wire_attr) is expected


async def test_restored_queue_crossfade_override_survives_the_global_default() -> None:
    """A restored queue's pinned crossfade override is never overruled by the global default."""
    stored = PlayerQueueData(
        queue=PlayerQueue(
            queue_id="p1",
            active=False,
            display_name="Player A",
            available=True,
            items=0,
        ),
        crossfade_override=True,
    )
    # the global default is "off"; the pinned override still wins
    queues = _controller(stored.to_cache(), crossfade_enabled=CONF_VALUE_DISABLED)

    await PlayerQueuesController.on_player_register(queues, _player())

    assert queues._queue_data["p1"].queue.crossfade_enabled is True


async def test_legacy_snapshot_diverging_from_global_gets_pinned() -> None:
    """A pre-override snapshot whose stored value diverges from the global gets pinned to it."""
    # the global default is off, but the legacy snapshot says on
    queues = _controller(
        _legacy_state(crossfade_enabled=True), crossfade_enabled=CONF_VALUE_DISABLED
    )

    await PlayerQueuesController.on_player_register(queues, _player())

    queue_data = queues._queue_data["p1"]
    assert queue_data.crossfade_override is True
    assert queue_data.queue.crossfade_enabled is True


async def test_legacy_snapshot_matching_global_stays_unpinned() -> None:
    """A pre-override snapshot whose stored value matches the global is left following it."""
    # the legacy snapshot's value already matches the global default: nothing to pin
    queues = _controller(
        _legacy_state(crossfade_enabled=True), crossfade_enabled=CONF_VALUE_ENABLED
    )

    await PlayerQueuesController.on_player_register(queues, _player())

    queue_data = queues._queue_data["p1"]
    assert queue_data.crossfade_override is None
    assert queue_data.queue.crossfade_enabled is True
