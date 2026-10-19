"""Tests for the playback restart that re-applies DSP when a player's settings change."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType

from music_assistant.controllers.players import PlayerController

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.fixture
def mock_mass() -> Iterator[MagicMock]:
    """Create a mock MusicAssistant instance whose DSP config reports DSP enabled."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    mass.config.get_player_dsp_config = MagicMock(return_value=SimpleNamespace(enabled=True))
    yield mass
    for scheduled in mass.create_task.call_args_list:
        target = scheduled.args[0] if scheduled.args else None
        if target is not None and hasattr(target, "close"):
            target.close()


def _player(player_id: str = "player_1") -> MagicMock:
    """Build a playing solo player that resolves to its own queue."""
    player = MagicMock()
    player.player_id = player_id
    player.display_name = player_id
    player.type = PlayerType.PLAYER
    player.extra_data = {}
    player.state.type = PlayerType.PLAYER
    player.state.playback_state = PlaybackState.PLAYING
    player.state.synced_to = None
    player.state.active_group = None
    player.state.active_source = player_id
    player.state.supported_features = set()
    return player


def _controller(mock_mass: MagicMock, *players: MagicMock) -> PlayerController:
    """Register the given players on a fresh controller."""
    controller = PlayerController(mock_mass)
    mock_mass.players = controller
    controller._players = {player.player_id: player for player in players}
    controller.cmd_stop = AsyncMock()  # type: ignore[method-assign]
    controller.cmd_play = AsyncMock()  # type: ignore[method-assign]
    return controller


class TestDspChangeRestart:
    """on_player_dsp_change restarts the queue the player is playing."""

    async def test_restart_skips_a_queue_emptied_while_waiting_for_the_lock(
        self, mock_mass: MagicMock
    ) -> None:
        """A queue transferred away while the restart waited for the lock is not resumed."""
        controller = _controller(mock_mass, _player())
        queue = MagicMock(queue_id="player_1", items=3)
        mock_mass.player_queues.get = MagicMock(return_value=queue)
        mock_mass.player_queues.resume = AsyncMock()

        @asynccontextmanager
        async def _lock_held_by_transfer(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
            queue.items = 0
            yield

        controller.get_group_and_player_lock = _lock_held_by_transfer  # type: ignore[method-assign]

        await controller.on_player_dsp_change("player_1")
        _delay, target, *args = mock_mass.call_later.call_args.args
        await target(*args)

        mock_mass.player_queues.resume.assert_not_awaited()
