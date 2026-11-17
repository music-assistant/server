"""
Tests for the restart of a flow stream a player lost.

The stream route and player providers ask the queue to restart when a player lost its
flow stream; the queue then resumes where it plays now, rate limited so a player that
keeps losing its stream does not restart playback over and over.
"""

from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.constants import (
    LOST_FLOW_RESTART_LIMIT,
    LOST_FLOW_RESTART_WINDOW,
)
from music_assistant.controllers.player_queues.state import PlayerQueueData

QUEUE_ID = "queue-1"


def _fake_controller() -> Any:
    """Build a stand-in for the controller, owning one queue that plays session-1."""
    fake = MagicMock()
    queue_data = PlayerQueueData(queue=MagicMock(queue_id=QUEUE_ID, display_name="Queue"))
    queue_data.session_id = "session-1"
    fake._queue_data = {QUEUE_ID: queue_data}
    fake.queue_data_or_none = MagicMock(side_effect=fake._queue_data.get)
    fake.mass.players.get_player.return_value.offline_by_power_control = False
    without_member_power_on = {"active": False}
    member_power_on = fake.mass.players.without_member_power_on.return_value
    member_power_on.__enter__.side_effect = lambda: without_member_power_on.update(active=True)
    member_power_on.__exit__.side_effect = lambda *_args: without_member_power_on.update(
        active=False
    )

    def _create_task(_coro: Any, **_kwargs: Any) -> None:
        # the restart is no command that starts a group: its switched-off members stay off
        assert without_member_power_on["active"]

    fake.mass.create_task = MagicMock(side_effect=_create_task)
    return fake


def _restart(fake: Any, session_id: str = "session-1") -> bool:
    return PlayerQueuesController.restart_lost_flow_stream(
        cast("PlayerQueuesController", fake), QUEUE_ID, session_id
    )


def _new_session(fake: Any, session_id: str) -> None:
    fake._queue_data[QUEUE_ID].session_id = session_id


def test_a_lost_flow_stream_resumes_the_queue_where_it_plays_now() -> None:
    """The queue resumes, which restarts the current item at the position that plays now."""
    fake = _fake_controller()

    assert _restart(fake) is True

    fake.resume.assert_called_once_with(QUEUE_ID, fade_in=False)
    fake.mass.create_task.assert_called_once()
    assert fake.mass.create_task.call_args.args[0] is fake.resume.return_value
    fake.logger.info.assert_called_once()


def test_a_session_restarts_once() -> None:
    """A second report of the same lost stream (a reconnect and an error) restarts nothing."""
    fake = _fake_controller()
    _restart(fake)

    assert _restart(fake) is True

    fake.resume.assert_called_once()
    assert len(fake._queue_data[QUEUE_ID].flow_restart_times) == 1


def test_restarts_stop_at_the_limit_until_the_window_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue restarts at most the limit within the window, and again once it passed."""
    fake = _fake_controller()
    now = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    for restart in range(LOST_FLOW_RESTART_LIMIT):
        _new_session(fake, f"session-{restart}")
        assert _restart(fake, f"session-{restart}") is True
        now += 10

    _new_session(fake, "session-over-limit")
    assert _restart(fake, "session-over-limit") is False
    assert fake.resume.call_count == LOST_FLOW_RESTART_LIMIT
    fake.logger.warning.assert_called_once()

    # the first restart leaves the window
    now = 1000.0 + LOST_FLOW_RESTART_WINDOW
    assert _restart(fake, "session-over-limit") is True
    assert fake.resume.call_count == LOST_FLOW_RESTART_LIMIT + 1


@pytest.mark.parametrize(
    "condition",
    ["another_session", "halted_by_command", "flow_fully_generated", "switched_off", "no_player"],
)
def test_no_restart(condition: str) -> None:
    """Only the current, playing session of a player that is on restarts."""
    fake = _fake_controller()
    queue_data = fake._queue_data[QUEUE_ID]
    session_id = "session-1"
    if condition == "another_session":
        session_id = "session-0"
    elif condition == "halted_by_command":
        queue_data.halted_by_command = True
    elif condition == "flow_fully_generated":
        queue_data.flow_buffer_completed = "session-1"
    elif condition == "switched_off":
        fake.mass.players.get_player.return_value.offline_by_power_control = True
    else:
        fake.mass.players.get_player.return_value = None

    assert _restart(fake, session_id) is False

    fake.resume.assert_not_called()
    fake.mass.create_task.assert_not_called()
    assert queue_data.flow_restart_times == []


def test_an_unknown_queue_restarts_nothing() -> None:
    """A queue that is gone has nothing to restart."""
    fake = _fake_controller()

    assert (
        PlayerQueuesController.restart_lost_flow_stream(
            cast("PlayerQueuesController", fake), "queue-gone", "session-1"
        )
        is False
    )
    fake.resume.assert_not_called()
