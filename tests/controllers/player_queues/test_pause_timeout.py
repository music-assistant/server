"""Tests for the pause timeout: how long a queue may stay paused before it is stopped."""

from __future__ import annotations

import inspect
from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.constants import CONF_PAUSE_TIMEOUT
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "player_1"


def _setup(pause_timeout: Any = None) -> tuple[PlayerQueuesController, MagicMock]:
    """
    Build a queue playing on a player that can pause.

    :param pause_timeout: The stored pause timeout, or None to leave it unset.
    """
    mass = MagicMock()

    def _core_config(_domain: str, key: str, default: Any = None) -> Any:
        if key == CONF_PAUSE_TIMEOUT and pause_timeout is not None:
            return pause_timeout
        return default

    mass.config.get_raw_core_config_value = MagicMock(side_effect=_core_config)
    player = MagicMock()
    player.offline_by_power_control = False
    player.extra_data = {}
    player.state.playback_state = PlaybackState.PLAYING
    mass.players.get_player = MagicMock(return_value=player)

    async def _device_pause(_queue_id: str) -> None:
        player.state.playback_state = PlaybackState.PAUSED

    mass.players._handle_cmd_pause = AsyncMock(side_effect=_device_pause)

    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = mass
    ctrl.logger = MagicMock()
    ctrl.stop = AsyncMock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q", available=True, items=1)
    queue.state = PlaybackState.PLAYING
    ctrl._queue_data = {QUEUE_ID: PlayerQueueData(queue=queue)}
    return ctrl, player


async def _pause_and_watch(ctrl: PlayerQueuesController, sleep: AsyncMock) -> None:
    """
    Pause the queue and run the pause watcher it started to completion.

    :param ctrl: The controller to pause the queue on.
    :param sleep: Stands in for asyncio.sleep while the watcher runs.
    """
    await ctrl.pause(QUEUE_ID)
    watchers: list[Coroutine[Any, Any, None]] = []
    for call in cast("MagicMock", ctrl.mass).create_task.call_args_list:
        target = call.args[0]
        if inspect.iscoroutine(target) and target.__name__ == "_watch_pause":
            watchers.append(target)
        elif inspect.iscoroutine(target):
            target.close()
    assert len(watchers) == 1
    with patch("music_assistant.controllers.player_queues.controller.asyncio.sleep", new=sleep):
        await watchers[0]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [(None, 30), (120, 120), (240, 240), (5, 30), (3600, 240)],
)
async def test_paused_queue_stops_after_the_pause_timeout(stored: Any, expected: int) -> None:
    """A queue that stays paused is stopped once the timeout passes, kept within its range."""
    ctrl, _player = _setup(stored)
    sleep = AsyncMock()

    await _pause_and_watch(ctrl, sleep)

    assert sleep.await_count == expected
    cast("AsyncMock", ctrl.stop).assert_awaited_once_with(QUEUE_ID)


async def test_queue_resumed_within_the_pause_timeout_is_not_stopped() -> None:
    """A queue that plays again before the timeout passes is left alone."""
    ctrl, player = _setup(120)

    async def _resume_at_100_seconds(_seconds: float) -> None:
        if sleep.await_count == 100:
            player.state.playback_state = PlaybackState.PLAYING

    sleep = AsyncMock(side_effect=_resume_at_100_seconds)

    await _pause_and_watch(ctrl, sleep)

    assert sleep.await_count == 100
    cast("AsyncMock", ctrl.stop).assert_not_awaited()
