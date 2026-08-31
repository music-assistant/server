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
    Build a queue-controller stand-in with the real toggle-resolution methods bound.

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
    # bind the real resolver/re-enqueue helpers so the methods under test exercise actual logic
    queues._resolve_default_toggles = lambda queue_data: (
        PlayerQueuesController._resolve_default_toggles(queues, queue_data)
    )
    queues._reenqueue_next_item_if_loaded = lambda queue_id: (
        PlayerQueuesController._reenqueue_next_item_if_loaded(queues, queue_id)
    )
    return queues


def _queue_data(
    queue_id: str = "q1",
    *,
    playing: bool = False,
    is_dynamic: bool = False,
    current_index: int | None = None,
    items: int = 2,
    **overrides: Any,
) -> PlayerQueueData:
    """Build a PlayerQueueData, optionally playing (with a loaded next item), near its end, or dynamic."""
    extra: dict[str, Any] = {"is_dynamic": is_dynamic}
    if playing:
        extra.update(state=PlaybackState.PLAYING, current_index=0, index_in_buffer=0)
    elif current_index is not None:
        extra["current_index"] = current_index
    queue = PlayerQueue(
        queue_id=queue_id, active=True, display_name="Q", available=True, items=items, **extra
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


async def test_update_config_reenqueues_next_item_only_for_the_queue_following_global() -> None:
    """A global crossfade flip re-enqueues the next item for a playing queue that follows it, not one pinned to a value."""
    queues = _controller(global_crossfade=False)
    following = _queue_data("q1", playing=True)  # crossfade_override=None -> follows global
    pinned = _queue_data("q2", playing=True, crossfade_override=True)  # already effectively True
    queues._queue_data["q1"] = following
    queues._queue_data["q2"] = pinned
    # resolve both against the initial global default (off) before the change under test
    queues._resolve_default_toggles(following)
    queues._resolve_default_toggles(pinned)

    next_item = SimpleNamespace(queue_item_id="next")
    queues.get_next_item = MagicMock(return_value=next_item)
    # flip the global default: the follow-global queue's effective value changes, the pinned one's doesn't
    queues.mass.config.get_raw_core_config_value.side_effect = lambda _core_module, _key, _default: (
        True
    )
    config = cast("CoreConfig", SimpleNamespace(values={}))

    await PlayerQueuesController.update_config(queues, config, {"values/crossfade_enabled"})

    assert following.queue.crossfade_enabled is True
    assert pinned.queue.crossfade_enabled is True
    queues._enqueue_next_item.assert_called_once_with("q1", next_item)


async def test_update_config_kicks_autoplay_refill_for_flipped_near_end_static_queue() -> None:
    """A global autoplay flip kicks a refill only for a following, non-dynamic queue near its end."""
    queues = _controller(global_autoplay=False)
    following = _queue_data("q1", current_index=8, items=10)  # follows global, 2 items left
    pinned_off = _queue_data("q2", current_index=8, items=10, autoplay_override=False)
    dynamic = _queue_data("q3", current_index=8, items=10, is_dynamic=True)
    for queue_data in (following, pinned_off, dynamic):
        queues._queue_data[queue_data.queue.queue_id] = queue_data
        # resolve against the initial global default (off) before the change under test
        queues._resolve_default_toggles(queue_data)

    # flip the global default on: the following and dynamic queues' effective values flip, the
    # pinned-off one's doesn't
    queues.mass.config.get_raw_core_config_value.side_effect = lambda _core_module, _key, _default: (
        True
    )
    config = cast("CoreConfig", SimpleNamespace(values={}))

    await PlayerQueuesController.update_config(queues, config, {"values/autoplay_enabled"})

    assert following.queue.autoplay_enabled is True
    assert pinned_off.queue.autoplay_enabled is False
    assert dynamic.queue.autoplay_enabled is True
    # only the following, non-dynamic, near-the-end queue gets the refill kick
    queues.mass.call_later.assert_called_once_with(
        5, queues._fill_autoplay_tracks, "q1", task_id="fill_autoplay_tracks_q1"
    )
