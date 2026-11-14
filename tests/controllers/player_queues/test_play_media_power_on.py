"""Queue commands on a player that is switched off at its power control."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import QueueOption

from music_assistant.controllers.player_queues import PlayerQueuesController


def _controller() -> Any:
    """Build a bare controller whose queue "q1" belongs to a switched off player."""
    ctrl: Any = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl.mass.players.get_player.return_value.offline_by_power_control = True
    ctrl.mass.players._resolve_playback_owner = lambda player: player  # plays on its own
    ctrl.mass.players._power_on_switched_off_player = AsyncMock()
    ctrl._check_player_permission = MagicMock()
    ctrl.get = MagicMock(return_value=MagicMock())
    ctrl._handle_play_media = AsyncMock()
    return ctrl


@pytest.mark.parametrize("option", [None, QueueOption.PLAY, QueueOption.REPLACE])
async def test_a_play_request_powers_the_player_on_first(option: QueueOption | None) -> None:
    """Media that starts playing powers the player on before the play action runs."""
    ctrl = _controller()
    steps: list[str] = []
    ctrl.mass.players._power_on_switched_off_player.side_effect = lambda _id: steps.append("on")
    ctrl._handle_play_media.side_effect = lambda *_args, **_kwargs: steps.append("play")

    await ctrl.play_media("q1", "library://track/1", option=option)

    assert steps == ["on", "play"]


@pytest.mark.parametrize("option", [QueueOption.ADD, QueueOption.NEXT, QueueOption.REPLACE_NEXT])
async def test_enqueued_media_leaves_the_player_switched_off(option: QueueOption) -> None:
    """Media that is only enqueued does not power the player on."""
    ctrl = _controller()

    await ctrl.play_media("q1", "library://track/1", option=option)

    ctrl.mass.players._power_on_switched_off_player.assert_not_awaited()
    ctrl._handle_play_media.assert_awaited_once()


async def test_pausing_a_switched_off_player_does_nothing() -> None:
    """A queue pause on a player switched off at its power control is a quiet no-op."""
    ctrl = _controller()
    ctrl.mass.players._handle_cmd_pause = AsyncMock()
    ctrl._set_transitioning = MagicMock()

    await ctrl.pause("q1")

    ctrl.mass.players._handle_cmd_pause.assert_not_awaited()


async def test_a_reachable_player_is_not_powered_on() -> None:
    """A player that is not switched off plays as before."""
    ctrl = _controller()
    ctrl.mass.players.get_player.return_value = Mock(offline_by_power_control=False)

    await ctrl.play_media("q1", "library://track/1")

    ctrl.mass.players._power_on_switched_off_player.assert_not_awaited()
