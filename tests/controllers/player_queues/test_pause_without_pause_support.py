"""
Tests for a queue paused on a player that can not pause.

The pause falls back to a stop of the queue, so its session, its item buffers and the
stream of the music source are released right away. A later play starts the queue again
at the paused position.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import Player
from tests.common import MockPlayer, MockProvider

QUEUE_ID = "player_1"


def _setup() -> tuple[PlayerQueuesController, MockPlayer, PlayerQueueData]:
    """Build a queue playing on its own player, which has no pause support."""
    mass = MagicMock()
    mass.closing = False
    mass.loop = None
    mass.config.get = MagicMock(return_value=[])
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")

    def _player_config(_player_id: str, _key: str, default: Any = None) -> Any:
        return default

    mass.config.get_raw_player_config_value = MagicMock(side_effect=_player_config)
    mass.get_providers = MagicMock(return_value=[])

    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = mass
    ctrl.logger = MagicMock()
    ctrl.signal_update = Mock()  # type: ignore[method-assign]
    ctrl.on_player_update = Mock()  # type: ignore[method-assign]
    ctrl._cleanup_queue_audio_data = Mock()  # type: ignore[method-assign]
    queue = PlayerQueue(queue_id=QUEUE_ID, active=True, display_name="Q", available=True, items=1)
    queue.state = PlaybackState.PLAYING
    queue.elapsed_time = 42
    queue.elapsed_time_last_updated = time.time()
    queue_data = PlayerQueueData(queue=queue)
    queue_data.session_id = "sess-1"
    ctrl._queue_data = {QUEUE_ID: queue_data}
    mass.player_queues = ctrl

    players = PlayerController(mass)
    mass.players = players
    player = MockPlayer(
        MockProvider("test_provider", instance_id="test_prov", mass=mass), QUEUE_ID, "Player"
    )
    player._attr_playback_state = PlaybackState.PLAYING
    player._attr_elapsed_time = 42
    player._attr_elapsed_time_last_updated = time.time()
    players._players = {QUEUE_ID: player}
    player.set_initialized()
    player.update_state(signal_event=False)

    async def _device_stop() -> None:
        player._attr_playback_state = PlaybackState.IDLE
        player._attr_elapsed_time = 0
        player.update_state(signal_event=False)

    player.stop = AsyncMock(side_effect=_device_stop)  # type: ignore[method-assign]
    return ctrl, player, queue_data


def _playback_state(player: Player) -> PlaybackState:
    """
    Return the playback state the player publishes.

    Read through a call, so a check after a state change is not narrowed by an earlier one.
    """
    return player.state.playback_state


def _take_pause_watcher(ctrl: PlayerQueuesController) -> Coroutine[Any, Any, None]:
    """
    Return the pause watcher the pause handed to create_task, closing any other coroutine.

    :param ctrl: The controller the pause ran on.
    """
    watchers: list[Coroutine[Any, Any, None]] = []
    for call in cast("MagicMock", ctrl.mass).create_task.call_args_list:
        target = call.args[0]
        if not inspect.iscoroutine(target):
            continue
        if target.__name__ == "_watch_pause":
            watchers.append(target)
        else:
            target.close()
    assert len(watchers) == 1
    return watchers[0]


async def test_pause_ends_the_queue_and_releases_its_session() -> None:
    """The queue session ends at once, the queue keeps the position to resume from."""
    ctrl, player, queue_data = _setup()
    mass = cast("MagicMock", ctrl.mass)

    await ctrl.pause(QUEUE_ID)

    cast("AsyncMock", player.stop).assert_awaited_once()
    assert queue_data.session_id is None
    mass.streams.audio_processing.clear.assert_called_once_with(QUEUE_ID, "sess-1")
    assert queue_data.queue.resume_pos == 42
    assert _playback_state(player) == PlaybackState.IDLE
    # the pause watcher finds nothing left to stop
    with patch(
        "music_assistant.controllers.player_queues.controller.asyncio.sleep", new=AsyncMock()
    ):
        await _take_pause_watcher(ctrl)
    cast("AsyncMock", player.stop).assert_awaited_once()
    mass.streams.audio_processing.clear.assert_called_once()


async def test_play_after_the_pause_starts_the_queue_at_the_paused_position() -> None:
    """Play on the stopped queue restarts its current item where the pause left it."""
    ctrl, _player, queue_data = _setup()
    item = MagicMock(queue_item_id="item-1", media_type=MediaType.TRACK)
    queue_data.items = [item]
    queue_data.queue.current_item = item
    await ctrl.pause(QUEUE_ID)
    _take_pause_watcher(ctrl).close()
    # the queue follows its player to idle (the tracker is stubbed out here)
    queue_data.queue.state = PlaybackState.IDLE
    ctrl.play_index = AsyncMock()  # type: ignore[method-assign]

    await ctrl._handle_play(QUEUE_ID)

    ctrl.play_index.assert_awaited_once_with(QUEUE_ID, "item-1", 42, False)
