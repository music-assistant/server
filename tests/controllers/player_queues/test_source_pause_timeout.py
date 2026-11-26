"""Tests for the pause timeout of a live source (such as Spotify Connect) paused on a player."""

from __future__ import annotations

import inspect
from collections.abc import Coroutine
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import PlaybackState
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.constants import CONF_PAUSE_TIMEOUT
from music_assistant.controllers.player_queues.state import PlayerQueueData

PLAYER_ID = "player_1"
SOURCE_URI = "spotify_connect://source/player_1"


def _setup(pause_timeout: int = 30) -> tuple[PlayerQueuesController, MagicMock, MagicMock]:
    """
    Build a player that plays a live source, paused.

    :param pause_timeout: The stored pause timeout.
    """
    mass = MagicMock()

    def _core_config(_domain: str, key: str, default: Any = None) -> Any:
        return pause_timeout if key == CONF_PAUSE_TIMEOUT else default

    mass.config.get_raw_core_config_value = MagicMock(side_effect=_core_config)
    player = MagicMock()
    player.player_id = PLAYER_ID
    player.name = "Kitchen"
    player.extra_data = {}
    player.playback_state = PlaybackState.PAUSED
    player.active_source = SOURCE_URI
    player.fake_paused = False
    session = MagicMock()
    session.provider_instance_id = "spotify_connect--test"
    session.source_id = PLAYER_ID
    session.playback_session_id = "sess1"
    mass.players.get_player = MagicMock(return_value=player)
    mass.players.get_audio_source_session = MagicMock(return_value=session)
    mass.players.cmd_stop = AsyncMock()
    mass.players.deselect_source = AsyncMock()

    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = mass
    ctrl.logger = MagicMock()
    queue = PlayerQueue(queue_id=PLAYER_ID, active=False, display_name="Q", available=True, items=0)
    queue.state = PlaybackState.IDLE
    ctrl._queue_data = {PLAYER_ID: PlayerQueueData(queue=queue)}
    return ctrl, player, session


def _take_watcher(ctrl: PlayerQueuesController) -> Coroutine[Any, Any, None] | None:
    """
    Return the source pause watcher the controller started, closing any other task.

    :param ctrl: The controller that was given the player update.
    """
    watcher = None
    for call in cast("MagicMock", ctrl.mass).create_task.call_args_list:
        target = call.args[0]
        if inspect.iscoroutine(target) and target.__name__ == "_watch_source_pause":
            assert watcher is None
            watcher = target
        elif inspect.iscoroutine(target):
            target.close()
    return watcher


async def _run(watcher: Coroutine[Any, Any, None] | None, sleep: AsyncMock) -> None:
    """
    Run a source pause watcher to completion.

    :param watcher: The watcher to run.
    :param sleep: Stands in for asyncio.sleep while the watcher runs.
    """
    assert watcher is not None
    with patch("music_assistant.controllers.player_queues.controller.asyncio.sleep", new=sleep):
        await watcher


async def test_a_paused_source_is_stopped_after_the_pause_timeout() -> None:
    """A player that stays paused on a live source is stopped once the pause timeout runs out."""
    ctrl, _player, _session = _setup(pause_timeout=120)
    sleep = AsyncMock()

    ctrl.on_player_update(cast("Any", _player), {})
    await _run(_take_watcher(ctrl), sleep)

    assert sleep.await_count == 120
    cast("AsyncMock", ctrl.mass.players.cmd_stop).assert_awaited_once_with(PLAYER_ID)
    # the source stays selected, so it can play on the player again later
    cast("AsyncMock", ctrl.mass.players.deselect_source).assert_not_awaited()


async def test_a_source_resumed_within_the_pause_timeout_is_kept() -> None:
    """A live source that plays again before the timeout keeps the player."""
    ctrl, player, _session = _setup()
    sleep = AsyncMock()

    async def _resume(_seconds: float) -> None:
        if sleep.await_count == 10:
            player.playback_state = PlaybackState.PLAYING

    sleep.side_effect = _resume

    ctrl.on_player_update(cast("Any", player), {})
    await _run(_take_watcher(ctrl), sleep)

    cast("AsyncMock", ctrl.mass.players.cmd_stop).assert_not_awaited()


async def test_a_source_replaced_during_the_pause_is_kept() -> None:
    """A reselected source is a new session: the old session's timeout does not stop it."""
    ctrl, player, session = _setup()
    sleep = AsyncMock()

    async def _reselect(_seconds: float) -> None:
        if sleep.await_count == 10:
            session.playback_session_id = "sess2"

    sleep.side_effect = _reselect

    ctrl.on_player_update(cast("Any", player), {})
    await _run(_take_watcher(ctrl), sleep)

    cast("AsyncMock", ctrl.mass.players.cmd_stop).assert_not_awaited()


async def test_a_player_without_a_live_source_starts_no_watcher() -> None:
    """A paused player with no live source session is left to its queue."""
    ctrl, player, _session = _setup()
    cast("MagicMock", ctrl.mass.players.get_audio_source_session).return_value = None

    ctrl.on_player_update(cast("Any", player), {})

    assert _take_watcher(ctrl) is None


async def test_a_player_shown_paused_starts_no_watcher() -> None:
    """A player stopped and shown as paused on the source ends that pause itself."""
    ctrl, player, _session = _setup()
    player.fake_paused = True

    ctrl.on_player_update(cast("Any", player), {})

    assert _take_watcher(ctrl) is None


async def test_a_playing_source_starts_no_watcher() -> None:
    """Only a paused live source is watched."""
    ctrl, player, _session = _setup()
    player.playback_state = PlaybackState.PLAYING

    ctrl.on_player_update(cast("Any", player), {})

    assert _take_watcher(ctrl) is None
