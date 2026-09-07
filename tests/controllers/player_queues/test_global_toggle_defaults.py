"""
Tests for the global autoplay/crossfade defaults and the per-queue overrides that pin them.

A queue's effective autoplay/crossfade follows the matching global core config value
(`autoplay_enabled`/`crossfade_enabled` on the `player_queues` core module) until its own toggle
command (`set_autoplay`/`set_crossfade`) pins an explicit override. The wire
`PlayerQueue.autoplay_enabled`/`crossfade_enabled` fields always carry that resolved effective
value.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

from music_assistant_models.enums import PlaybackState

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller(*, global_autoplay: bool = True, global_crossfade: bool = False) -> Any:
    """
    Build a queue-controller stand-in with the real toggle resolver bound.

    Spec'd to `PlayerQueuesController` (rather than a bare MagicMock) so `isinstance` checks -
    like the zero-arg `super()` call inside `update_config` - resolve correctly.

    :param global_autoplay: The stubbed global autoplay_enabled core config value.
    :param global_crossfade: The stubbed global crossfade_enabled core config value.
    """
    queues = MagicMock(spec=PlayerQueuesController)
    queues._queue_data = {}
    queues.mass = MagicMock()
    defaults = {"autoplay_enabled": global_autoplay, "crossfade_enabled": global_crossfade}
    queues.mass.config.get_raw_core_config_value = MagicMock(
        side_effect=lambda _core_module, key, _default: defaults[key]
    )
    # bind the real resolver so the methods under test exercise actual logic
    queues._resolve_default_toggles = lambda queue_data: (
        PlayerQueuesController._resolve_default_toggles(queues, queue_data)
    )
    return queues


def _queue_data(
    queue_id: str = "q1", *, playing: bool = False, **overrides: Any
) -> PlayerQueueData:
    """Build a PlayerQueueData, optionally set up as actively playing with a loaded next item."""
    extra: dict[str, Any] = {}
    if playing:
        extra.update(state=PlaybackState.PLAYING, current_index=0, index_in_buffer=0)
    queue = PlayerQueue(
        queue_id=queue_id, active=True, display_name="Q", available=True, items=2, **extra
    )
    return PlayerQueueData(queue=queue, **overrides)


async def test_set_autoplay_pins_override_unaffected_by_later_global_change() -> None:
    """set_autoplay pins an explicit override that a later global default change can't undo."""
    queues = _controller(global_autoplay=True)
    queue_data = _queue_data()
    queues._queue_data["q1"] = queue_data

    PlayerQueuesController.set_autoplay(queues, "q1", False)

    assert queue_data.autoplay_override is False
    assert queue_data.queue.autoplay_enabled is False

    # the global default flips, but the pinned override still wins on the next resolution
    queues.mass.config.get_raw_core_config_value.side_effect = lambda _core_module, _key, _default: (
        True
    )
    queues._resolve_default_toggles(queue_data)

    assert queue_data.queue.autoplay_enabled is False


async def test_set_crossfade_pins_override_unaffected_by_later_global_change() -> None:
    """set_crossfade pins an explicit override that a later global default change can't undo."""
    queues = _controller(global_crossfade=False)
    queue_data = _queue_data()
    queues._queue_data["q1"] = queue_data

    PlayerQueuesController.set_crossfade(queues, "q1", True)

    assert queue_data.crossfade_override is True
    assert queue_data.queue.crossfade_enabled is True

    queues.mass.config.get_raw_core_config_value.side_effect = lambda _core_module, _key, _default: (
        False
    )
    queues._resolve_default_toggles(queue_data)

    assert queue_data.queue.crossfade_enabled is True


async def test_set_crossfade_pins_override_matching_the_global_default() -> None:
    """Setting crossfade to the value it already effectively has still pins an explicit override."""
    queues = _controller(global_crossfade=False)
    queue_data = _queue_data()  # crossfade_override=None, effective False (follows global)
    queues._queue_data["q1"] = queue_data

    PlayerQueuesController.set_crossfade(queues, "q1", False)

    # explicitly pinned now, no longer None/"follow global"
    assert queue_data.crossfade_override is False
    assert queue_data.queue.crossfade_enabled is False
    # the pin must persist even though the effective value didn't change
    queues.signal_update.assert_called_once_with("q1")
    # no audible change occurred, so no re-enqueue side effect
    queues._enqueue_next_item.assert_not_called()


async def test_update_config_re_resolves_following_queues_but_not_pinned_ones() -> None:
    """A global default change reaches follow-global queues live; pinned queues keep their value."""
    queues = _controller(global_autoplay=False, global_crossfade=False)
    following = _queue_data("q1", playing=True)
    pinned = _queue_data("q2", playing=True, autoplay_override=False, crossfade_override=False)
    for queue_data in (following, pinned):
        queues._queue_data[queue_data.queue.queue_id] = queue_data
        # resolve against the initial global defaults (off) before the change under test
        queues._resolve_default_toggles(queue_data)

    # flip both global defaults on: only the follow-global queue's effective values change
    queues.mass.config.get_raw_core_config_value.side_effect = lambda _core_module, _key, _default: (
        True
    )
    config = cast("CoreConfig", SimpleNamespace(values={}))

    await PlayerQueuesController.update_config(
        queues, config, {"values/autoplay_enabled", "values/crossfade_enabled"}
    )

    assert following.queue.autoplay_enabled is True
    assert following.queue.crossfade_enabled is True
    assert pinned.queue.autoplay_enabled is False
    assert pinned.queue.crossfade_enabled is False
    # every queue is signalled; the audible effect lands on the next transition, so there is
    # no immediate re-enqueue or refill kick
    assert queues.signal_update.call_count == 2
    queues._enqueue_next_item.assert_not_called()
    queues.mass.call_later.assert_not_called()
