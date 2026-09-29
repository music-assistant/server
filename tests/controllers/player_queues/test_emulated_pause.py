"""
Tests for a queue paused on a player that can not pause.

The device is stopped and the player reports paused. The queue keeps its session, so
play restarts it quickly at the paused position, until the pause watcher ends the queue
like any other pause that lasts too long.
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from music_assistant_models.enums import PlaybackState
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


async def test_pause_watcher_ends_the_queue_and_releases_its_session() -> None:
    """After thirty seconds of the emulated pause, the queue is stopped for real."""
    ctrl, player, queue_data = _setup()
    mass = cast("MagicMock", ctrl.mass)

    await ctrl.pause(QUEUE_ID)
    assert _playback_state(player) == PlaybackState.PAUSED
    # the session stays open while paused, so play can restart from the buffered audio
    session_while_paused = queue_data.session_id
    assert session_while_paused == "sess-1"
    mass.streams.audio_processing.clear.assert_not_called()
    watcher = mass.create_task.call_args.args[0]
    with patch(
        "music_assistant.controllers.player_queues.controller.asyncio.sleep", new=AsyncMock()
    ):
        await watcher

    assert _playback_state(player) == PlaybackState.IDLE
    assert not player.emulated_pause
    assert queue_data.session_id is None
    mass.streams.audio_processing.clear.assert_called_once_with(QUEUE_ID, "sess-1")
    cast("AsyncMock", player.stop).assert_awaited_once()


async def test_play_resumes_the_queue_instead_of_unpausing_the_device() -> None:
    """The stopped device has nothing to unpause, the queue restarts at the saved position."""
    ctrl, player, queue_data = _setup()
    await ctrl.pause(QUEUE_ID)
    # the queue follows the paused player (the tracker is stubbed out here)
    queue_data.queue.state = PlaybackState.PAUSED
    ctrl.resume = AsyncMock()  # type: ignore[method-assign]
    player.play = AsyncMock()  # type: ignore[method-assign]

    await ctrl._handle_play(QUEUE_ID)

    ctrl.resume.assert_awaited_once_with(QUEUE_ID)
    player.play.assert_not_awaited()
    assert queue_data.queue.resume_pos == 42
