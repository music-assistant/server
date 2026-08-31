"""
Tests for how a changed crossfade/autoplay global default reaches existing queues.

`update_config` refreshes every queue's effective toggle when the global default changes; the OSD
commands (`set_crossfade` / `set_autoplay`) pin a queue's own override, even when that does not
move the queue's effective value.
"""

from __future__ import annotations

from types import MethodType
from typing import Any
from unittest.mock import MagicMock

from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import CONF_VALUE_ENABLED
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller(**global_defaults: str) -> Any:
    """
    Build a controller stand-in with the real toggle-resolution logic bound onto it.

    :param global_defaults: The value ``get_raw_core_config_value`` should return for a given
        config key (e.g. ``crossfade_enabled="enabled"``); a key not given falls back to the
        default value the caller passed in.
    """
    controller = MagicMock()
    # update_config calls super().update_config(...), which needs a real isinstance check
    controller.__class__ = PlayerQueuesController  # type: ignore[assignment]
    controller._queue_data = {}
    controller.mass.config.get_raw_core_config_value = MagicMock(
        side_effect=lambda _domain, key, default: global_defaults.get(key, default)
    )
    for name in (
        "_global_toggle_default",
        "_apply_toggle_state",
        "update_config",
        "set_autoplay",
        "set_crossfade",
    ):
        setattr(controller, name, MethodType(getattr(PlayerQueuesController, name), controller))
    return controller


def _queue_data(queue_id: str, **overrides: Any) -> PlayerQueueData:
    """Build a PlayerQueueData with the given overrides, ready to attach to a fake controller."""
    return PlayerQueueData(
        queue=PlayerQueue(
            queue_id=queue_id, active=True, display_name=queue_id, available=True, items=0
        ),
        **overrides,
    )


async def test_update_config_moves_a_following_queue_and_leaves_a_pinned_one() -> None:
    """A global default change updates a following queue and leaves a pinned one alone."""
    controller = _controller(crossfade_enabled=CONF_VALUE_ENABLED)
    following = _queue_data("following")
    following.queue.crossfade_enabled = False  # simulate the old ("disabled") global
    pinned = _queue_data("pinned", crossfade_override=False)
    pinned.queue.crossfade_enabled = False
    controller._queue_data = {"following": following, "pinned": pinned}

    await controller.update_config(MagicMock(), {"values/crossfade_enabled"})

    assert following.queue.crossfade_enabled is True  # moved with the new global
    assert pinned.queue.crossfade_enabled is False  # stayed pinned regardless of the global


def test_set_crossfade_pins_a_queue_that_was_following_the_global_at_the_same_value() -> None:
    """Setting crossfade to its already-effective value still pins the override and persists."""
    controller = _controller(crossfade_enabled=CONF_VALUE_ENABLED)
    queue_data = _queue_data("q1")
    queue_data.queue.crossfade_enabled = True  # already matches the (unpinned) global default
    controller._queue_data = {"q1": queue_data}

    controller.set_crossfade("q1", True)

    assert queue_data.crossfade_override is True
    assert queue_data.queue.crossfade_enabled is True
    controller.signal_update.assert_called_once_with("q1")
    # the effective value did not move, so no transition needs to be refreshed
    controller._refresh_enqueued_next_item.assert_not_called()


def test_set_autoplay_pins_a_queue_that_was_following_the_global_at_the_same_value() -> None:
    """Setting Autoplay to its already-effective value still pins the override and persists."""
    controller = _controller(autoplay_enabled=CONF_VALUE_ENABLED)
    queue_data = _queue_data("q1")
    queue_data.queue.autoplay_enabled = True  # already matches the (unpinned) global default
    controller._queue_data = {"q1": queue_data}

    controller.set_autoplay("q1", True)

    assert queue_data.autoplay_override is True
    assert queue_data.queue.autoplay_enabled is True
    controller.signal_update.assert_called_once_with(queue_id="q1")
    # the effective value did not move, so no refill needs to be kicked off
    controller._kick_autoplay_refill.assert_not_called()
