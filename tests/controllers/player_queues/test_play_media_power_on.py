"""Queue commands on a player that is switched off at its power control."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from music_assistant_models.enums import PlaybackState, PlayerType, QueueOption
from music_assistant_models.errors import PlayerUnavailableError
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.constants import ATTR_PLAY_ACTION_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller() -> Any:
    """Build a bare controller whose queue "q1" belongs to a switched off player."""
    ctrl: Any = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.mass = MagicMock()
    ctrl._queue_data = {}
    ctrl.mass.players.resolve_player_id = lambda player_id: player_id
    ctrl.mass.players.get_player.return_value.offline_by_power_control = True
    ctrl.mass.players._resolve_playback_owner = lambda player: player  # plays on its own
    ctrl.mass.players._power_on_switched_off_player = AsyncMock()
    ctrl._check_player_permission = MagicMock()
    ctrl._get_default_play_option = AsyncMock(return_value=QueueOption.PLAY)
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


@pytest.mark.parametrize("default", [QueueOption.ADD, QueueOption.NEXT])
async def test_media_that_is_only_enqueued_by_default_leaves_the_player_switched_off(
    default: QueueOption,
) -> None:
    """A request without an option whose default only enqueues does not power the player on."""
    ctrl = _controller()
    ctrl._get_default_play_option.return_value = default

    await ctrl.play_media("q1", "library://track/1")

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


def _flagged(ctrl: Any) -> bool:
    """Return whether queue "q1" shows a play action."""
    queue = ctrl._queue_data["q1"].queue
    return queue.extra_attributes.get(ATTR_PLAY_ACTION_IN_PROGRESS, False) is True


def _with_queue(ctrl: Any, state: PlaybackState) -> Any:
    """Give the controller a real record for queue "q1", in the given state."""
    queue = PlayerQueue(queue_id="q1", active=True, display_name="Q1", available=True, items=0)
    queue.state = state
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    ctrl.signal_update = MagicMock()
    ctrl.on_player_update = MagicMock()
    return ctrl


@pytest.mark.parametrize("wait", ["starts up", "fails"])
async def test_a_play_request_shows_while_the_player_starts_up(wait: str) -> None:
    """
    A play request on a queue that does not play shows from the start of the power on.

    It shows until the play action ended, and no more when the power on failed.

    :param wait: How the wait for the player ends.
    """
    ctrl = _with_queue(_controller(), PlaybackState.IDLE)
    seen: list[tuple[str, bool]] = []

    def _power_on(_player_id: str) -> None:
        seen.append(("power on", _flagged(ctrl)))
        if wait == "fails":
            raise PlayerUnavailableError("Player q1 could not be reached")

    ctrl.mass.players._power_on_switched_off_player.side_effect = _power_on
    ctrl._handle_play_media.side_effect = lambda *_args: seen.append(("play", _flagged(ctrl)))

    if wait == "fails":
        with pytest.raises(PlayerUnavailableError):
            await ctrl.play_media("q1", "library://track/1")
    else:
        await ctrl.play_media("q1", "library://track/1")

    expected = [("power on", True)] if wait == "fails" else [("power on", True), ("play", True)]
    assert seen == expected, f"the play request does not show while the player starts up: {seen}"
    assert not _flagged(ctrl)
    assert ctrl._queue_data["q1"].play_action_refcount == 0


@pytest.mark.parametrize("state", [PlaybackState.IDLE, PlaybackState.PLAYING])
async def test_a_play_request_on_a_group_shows_while_it_waits_for_a_member_unless_it_plays(
    state: PlaybackState,
) -> None:
    """
    A play request on a group shows during the wait for its members while the group does not play.

    :param state: What the queue of the group does when the request arrives.
    """
    ctrl = _with_queue(_controller(), state)
    ctrl.mass.players.get_player.return_value = Mock(
        offline_by_power_control=False, type=PlayerType.GROUP
    )
    ctrl.mass.players._members_to_power_on = MagicMock(return_value=["den"])
    during_wait: list[bool] = []
    ctrl.mass.players._power_on_members = AsyncMock(
        side_effect=lambda _group: during_wait.append(_flagged(ctrl))
    )

    await ctrl.play_media("q1", "library://track/1")

    shows = state != PlaybackState.PLAYING
    assert during_wait == [shows], (
        f"the {state.value} group shows the play request while it waits for a member: "
        f"{during_wait[0]}, expected {shows}"
    )
    assert not _flagged(ctrl)
    assert ctrl._queue_data["q1"].play_action_refcount == 0
