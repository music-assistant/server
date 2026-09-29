"""
Tests for a queue paused on a player that can not pause.

The device is stopped and the player reports paused. The queue keeps its session, so
play restarts it quickly at the paused position, until the pause watcher ends the queue
like any other pause that lasts too long.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.models.player import Player
from tests.common import MockPlayer, MockProvider

QUEUE_ID = "player_1"


def _setup(
    player_type: PlayerType = PlayerType.PLAYER,
) -> tuple[PlayerQueuesController, MockPlayer, PlayerQueueData]:
    """
    Build a queue playing on its own player, which has no pause support.

    :param player_type: The type of the player the queue plays on.
    """
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
        MockProvider("test_provider", instance_id="test_prov", mass=mass),
        QUEUE_ID,
        "Player",
        player_type=player_type,
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


def _pause_watchers(ctrl: PlayerQueuesController) -> list[Coroutine[Any, Any, None]]:
    """
    Return the pause watchers the pause handed to create_task, closing any other coroutine.

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
    return watchers


def _take_pause_watcher(ctrl: PlayerQueuesController) -> Coroutine[Any, Any, None]:
    """
    Return the one pause watcher the pause started.

    :param ctrl: The controller the pause ran on.
    """
    watchers = _pause_watchers(ctrl)
    assert len(watchers) == 1
    return watchers[0]


async def _run_pause_watcher(ctrl: PlayerQueuesController, sleep: AsyncMock | None = None) -> None:
    """
    Run the pause watcher to its end, without waiting out its sleeps.

    :param ctrl: The controller the pause ran on.
    :param sleep: The stand-in for the watcher's sleeps, to count or act on them.
    """
    with patch(
        "music_assistant.controllers.player_queues.controller.asyncio.sleep",
        new=sleep or AsyncMock(),
    ):
        await _take_pause_watcher(ctrl)


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
    await _run_pause_watcher(ctrl)

    assert _playback_state(player) == PlaybackState.IDLE
    assert not player.emulated_pause
    assert queue_data.session_id is None
    mass.streams.audio_processing.clear.assert_called_once_with(QUEUE_ID, "sess-1")
    cast("AsyncMock", player.stop).assert_awaited_once()


async def test_pause_watcher_steps_aside_once_the_session_changed() -> None:
    """A resume that is still starting keeps its new session, the watcher leaves at once."""
    ctrl, player, queue_data = _setup()
    mass = cast("MagicMock", ctrl.mass)
    await ctrl.pause(QUEUE_ID)

    async def _resume_after_three_seconds(_seconds: float) -> None:
        if sleep.await_count == 3:
            # the resume started a new session, the device does not report playing yet
            queue_data.session_id = "sess-2"

    sleep = AsyncMock(side_effect=_resume_after_three_seconds)
    await _run_pause_watcher(ctrl, sleep)

    assert sleep.await_count == 3
    assert queue_data.session_id == "sess-2"
    mass.streams.audio_processing.clear.assert_not_called()
    cast("AsyncMock", player.stop).assert_awaited_once()


async def test_pause_watcher_ends_quietly_when_the_queue_is_removed() -> None:
    """A player removed while paused takes its queue along, there is nothing left to stop."""
    ctrl, player, _queue_data = _setup()
    mass = cast("MagicMock", ctrl.mass)
    await ctrl.pause(QUEUE_ID)

    async def _remove_the_player(_seconds: float) -> None:
        if sleep.await_count == 3:
            ctrl._queue_data.pop(QUEUE_ID)

    sleep = AsyncMock(side_effect=_remove_the_player)
    await _run_pause_watcher(ctrl, sleep)

    assert sleep.await_count == 3
    mass.streams.audio_processing.clear.assert_not_called()
    cast("AsyncMock", player.stop).assert_awaited_once()


async def test_pause_watcher_leaves_a_restart_during_the_device_stop_alone() -> None:
    """A resume that starts while the pause still waits for the device keeps its session."""
    ctrl, player, queue_data = _setup()
    mass = cast("MagicMock", ctrl.mass)
    device_stop = cast("AsyncMock", player.stop).side_effect

    async def _resume_during_the_stop() -> None:
        # the pause does not hold the playback lock, so a resume can start meanwhile
        queue_data.session_id = "sess-2"
        await device_stop()

    player.stop = AsyncMock(side_effect=_resume_during_the_stop)  # type: ignore[method-assign]
    await ctrl.pause(QUEUE_ID)

    await _run_pause_watcher(ctrl)

    assert queue_data.session_id == "sess-2"
    mass.streams.audio_processing.clear.assert_not_called()
    player.stop.assert_awaited_once()


async def test_play_pause_before_the_device_confirms_its_stop_plays() -> None:
    """A second tap while the stopped device still reports playing resumes the queue."""
    ctrl, player, _queue_data = _setup()
    player.stop = AsyncMock()  # type: ignore[method-assign]
    await ctrl.pause(QUEUE_ID)
    _take_pause_watcher(ctrl).close()
    # the queue follows the player it plays on
    PlayerQueuesController.on_player_update(ctrl, player, {})
    ctrl.pause = AsyncMock()  # type: ignore[method-assign]
    ctrl.play = AsyncMock()  # type: ignore[method-assign]

    await ctrl.play_pause(QUEUE_ID)

    ctrl.play.assert_awaited_once_with(QUEUE_ID)
    ctrl.pause.assert_not_awaited()


async def test_pause_on_a_group_ends_its_queue_at_once() -> None:
    """A group can not resume as the same group, so its session is released right away."""
    ctrl, player, queue_data = _setup(PlayerType.GROUP)
    mass = cast("MagicMock", ctrl.mass)

    await ctrl.pause(QUEUE_ID)

    assert _playback_state(player) == PlaybackState.IDLE
    assert not player.emulated_pause
    assert queue_data.session_id is None
    mass.streams.audio_processing.clear.assert_called_once_with(QUEUE_ID, "sess-1")
    await _run_pause_watcher(ctrl)
    cast("AsyncMock", player.stop).assert_awaited_once()
    mass.streams.audio_processing.clear.assert_called_once()


async def test_every_emulated_pause_gets_a_pause_watcher() -> None:
    """The watcher is what ends an emulated pause, so one is started for each of them."""
    ctrl, player, queue_data = _setup()
    # the queue's own flag lags behind the player it plays on
    queue_data.queue.active = False

    await ctrl.pause(QUEUE_ID)

    assert player.emulated_pause
    _take_pause_watcher(ctrl).close()


async def test_pause_during_an_announcement_is_a_plain_stop() -> None:
    """Without a pause watcher to end it, a pause is not emulated."""
    ctrl, player, _queue_data = _setup()
    player.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True

    await ctrl.pause(QUEUE_ID)

    cast("AsyncMock", player.stop).assert_awaited_once()
    assert not player.emulated_pause
    assert _playback_state(player) == PlaybackState.IDLE
    assert _pause_watchers(ctrl) == []


async def test_play_resumes_the_queue_instead_of_unpausing_the_device() -> None:
    """The stopped device has nothing to unpause, the queue restarts at the saved position."""
    ctrl, player, queue_data = _setup()
    await ctrl.pause(QUEUE_ID)
    _take_pause_watcher(ctrl).close()
    # the queue follows the paused player (the tracker is stubbed out here)
    queue_data.queue.state = PlaybackState.PAUSED
    ctrl.resume = AsyncMock()  # type: ignore[method-assign]
    player.play = AsyncMock()  # type: ignore[method-assign]

    await ctrl._handle_play(QUEUE_ID)

    ctrl.resume.assert_awaited_once_with(QUEUE_ID)
    player.play.assert_not_awaited()
    assert queue_data.queue.resume_pos == 42
