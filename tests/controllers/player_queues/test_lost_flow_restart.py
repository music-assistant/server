"""
Tests for the restart of a flow stream a player lost.

The stream route and player providers ask the queue to restart when a player lost its
flow stream; the queue then resumes where it plays now, rate limited so a player that
keeps losing its stream does not restart playback over and over.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from functools import partial
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
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
    fake._lost_flow_stream_restartable = partial(
        PlayerQueuesController._lost_flow_stream_restartable, fake
    )
    fake._player_plays_queue = partial(PlayerQueuesController._player_plays_queue, fake)
    player = fake.mass.players.get_player.return_value
    player.offline_by_power_control = False
    player.available = True
    player.playback_state = PlaybackState.PLAYING
    player.active_source = QUEUE_ID
    player.extra_data = {}
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


def _restart(
    fake: Any,
    session_id: str = "session-1",
    only_while_playing: bool = False,
    queue_item_id: str | None = None,
) -> bool:
    return PlayerQueuesController.restart_lost_flow_stream(
        cast("PlayerQueuesController", fake),
        QUEUE_ID,
        session_id,
        only_while_playing=only_while_playing,
        queue_item_id=queue_item_id,
    )


def _new_session(fake: Any, session_id: str) -> None:
    fake._queue_data[QUEUE_ID].session_id = session_id


def test_a_lost_flow_stream_resumes_the_queue_where_it_plays_now() -> None:
    """The queue resumes, which restarts the current item at the position that plays now."""
    fake = _fake_controller()

    assert _restart(fake) is True

    fake._restart_lost_flow_stream.assert_called_once_with(
        QUEUE_ID, "session-1", only_while_playing=False, queue_item_id=None
    )
    fake.mass.create_task.assert_called_once()
    assert fake.mass.create_task.call_args.args[0] is fake._restart_lost_flow_stream.return_value
    fake.logger.info.assert_called_once()


def test_a_session_restarts_once() -> None:
    """A second report of the same lost stream (a reconnect and an error) restarts nothing."""
    fake = _fake_controller()
    _restart(fake)

    assert _restart(fake) is True

    fake._restart_lost_flow_stream.assert_called_once()
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
    assert fake._restart_lost_flow_stream.call_count == LOST_FLOW_RESTART_LIMIT
    fake.logger.warning.assert_called_once()

    # the first restart leaves the window
    now = 1000.0 + LOST_FLOW_RESTART_WINDOW
    assert _restart(fake, "session-over-limit") is True
    assert fake._restart_lost_flow_stream.call_count == LOST_FLOW_RESTART_LIMIT + 1


@pytest.mark.parametrize(
    "condition",
    [
        "another_session",
        "halted_by_command",
        "flow_fully_generated",
        "switched_off",
        "no_player",
        "pending_step",
        "another_item",
    ],
)
def test_no_restart(condition: str) -> None:
    """Only the current, playing session of a player that is on restarts."""
    fake = _fake_controller()
    queue_data = fake._queue_data[QUEUE_ID]
    queue_data.queue.current_item.queue_item_id = "item-1"
    session_id = "session-1"
    if condition == "another_session":
        session_id = "session-0"
    elif condition == "halted_by_command":
        queue_data.halted_by_command = True
    elif condition == "flow_fully_generated":
        queue_data.flow_buffer_completed = "session-1"
    elif condition == "switched_off":
        fake.mass.players.get_player.return_value.offline_by_power_control = True
    elif condition == "pending_step":
        # a next or previous waits out its debounce, or a start did not play yet
        queue_data.transitioning = True
    elif condition == "another_item":
        # the lost stream of a single item, while the queue moved on to another item
        queue_data.queue.current_item.queue_item_id = "item-2"
    else:
        fake.mass.players.get_player.return_value = None

    assert _restart(fake, session_id, queue_item_id="item-1") is False

    fake._restart_lost_flow_stream.assert_not_called()
    fake.mass.create_task.assert_not_called()
    assert queue_data.flow_restart_times == []


def test_a_stream_the_player_closed_restarts_a_queue_it_plays() -> None:
    """A player that still plays its queue after it closed its stream restarts."""
    fake = _fake_controller()

    assert _restart(fake, only_while_playing=True) is True

    fake._restart_lost_flow_stream.assert_called_once_with(
        QUEUE_ID, "session-1", only_while_playing=True, queue_item_id=None
    )


@pytest.mark.parametrize(
    "condition",
    [
        "paused",
        "idle",
        "inactive",
        "unavailable",
        "announcement",
        "another_session",
        "halted_by_command",
        "flow_fully_generated",
        "switched_off",
        "no_player",
    ],
)
def test_no_restart_for_a_stream_the_player_closed(condition: str) -> None:
    """Only a player that is available and plays its queue, without an announcement, restarts."""
    fake = _fake_controller()
    queue_data = fake._queue_data[QUEUE_ID]
    player = fake.mass.players.get_player.return_value
    session_id = "session-1"
    if condition == "paused":
        player.playback_state = PlaybackState.PAUSED
    elif condition == "idle":
        player.playback_state = PlaybackState.IDLE
    elif condition == "inactive":
        # another app took over the player
        player.active_source = "spotify_connect"
    elif condition == "unavailable":
        player.available = False
    elif condition == "announcement":
        player.extra_data = {ATTR_ANNOUNCEMENT_IN_PROGRESS: True}
    elif condition == "another_session":
        session_id = "session-0"
    elif condition == "halted_by_command":
        queue_data.halted_by_command = True
    elif condition == "flow_fully_generated":
        queue_data.flow_buffer_completed = "session-1"
    elif condition == "switched_off":
        player.offline_by_power_control = True
    else:
        fake.mass.players.get_player.return_value = None

    assert _restart(fake, session_id, only_while_playing=True) is False

    fake._restart_lost_flow_stream.assert_not_called()
    assert queue_data.flow_restart_times == []


def test_a_receiver_that_errors_while_idle_still_restarts() -> None:
    """The reconnect and the error report of a receiver come while it does not play."""
    fake = _fake_controller()
    fake.mass.players.get_player.return_value.playback_state = PlaybackState.IDLE

    assert _restart(fake) is True

    fake._restart_lost_flow_stream.assert_called_once()


@pytest.mark.parametrize("reconnect_first", [True, False], ids=["reconnect_first", "wait_first"])
def test_a_closed_stream_and_its_reconnect_restart_once(reconnect_first: bool) -> None:
    """A player that closed its stream and then asked to continue it restarts once."""
    fake = _fake_controller()
    reports = [partial(_restart, fake), partial(_restart, fake, only_while_playing=True)]
    if not reconnect_first:
        reports.reverse()

    for report in reports:
        report()

    fake._restart_lost_flow_stream.assert_called_once()
    assert len(fake._queue_data[QUEUE_ID].flow_restart_times) == 1


def test_an_unknown_queue_restarts_nothing() -> None:
    """A queue that is gone has nothing to restart."""
    fake = _fake_controller()

    assert (
        PlayerQueuesController.restart_lost_flow_stream(
            cast("PlayerQueuesController", fake), "queue-gone", "session-1"
        )
        is False
    )
    fake._restart_lost_flow_stream.assert_not_called()


def test_each_session_restarts_in_a_task_of_its_own() -> None:
    """A newer session that loses its stream while the previous restart runs still restarts."""
    fake = _fake_controller()
    _restart(fake)
    _new_session(fake, "session-2")

    assert _restart(fake, "session-2") is True

    task_ids = [call.kwargs["task_id"] for call in fake.mass.create_task.call_args_list]
    assert len(set(task_ids)) == 2


@pytest.mark.parametrize("command", [None, "stop", "next"])
async def test_the_restart_yields_to_a_command_that_went_first(command: str | None) -> None:
    """The queue resumes under the playback lock, unless a command that held it went first."""
    fake = _fake_controller()
    queue_data = fake._queue_data[QUEUE_ID]
    lock_held = {"held": False}

    @contextlib.asynccontextmanager
    async def _playback_lock(_queue_id: str) -> AsyncIterator[None]:
        # the command that held the lock while the restart waited for it ran first
        if command == "stop":
            queue_data.halted_by_command = True
            queue_data.session_id = None
        elif command == "next":
            queue_data.session_id = "session-2"
        lock_held["held"] = True
        yield
        lock_held["held"] = False

    resumed_under_lock: list[bool] = []
    fake.mass.players.get_group_and_player_lock = _playback_lock
    fake.resume = AsyncMock(
        side_effect=lambda *_args, **_kwargs: resumed_under_lock.append(lock_held["held"])
    )

    await PlayerQueuesController._restart_lost_flow_stream(
        cast("PlayerQueuesController", fake), QUEUE_ID, "session-1"
    )

    if command is None:
        fake.resume.assert_awaited_once_with(QUEUE_ID, fade_in=False)
        assert resumed_under_lock == [True]
    else:
        fake.resume.assert_not_awaited()


@pytest.mark.parametrize("only_while_playing", [True, False], ids=["closed_stream", "error"])
async def test_a_restart_waiting_for_the_lock_rechecks_a_closed_stream(
    only_while_playing: bool,
) -> None:
    """A pause at the device while the restart waited stops it, unless the receiver errored."""
    fake = _fake_controller()

    @contextlib.asynccontextmanager
    async def _playback_lock(_queue_id: str) -> AsyncIterator[None]:
        # the player reported a pause from its device while the restart waited for the lock
        fake.mass.players.get_player.return_value.playback_state = PlaybackState.PAUSED
        yield

    fake.mass.players.get_group_and_player_lock = _playback_lock
    fake.resume = AsyncMock()

    await PlayerQueuesController._restart_lost_flow_stream(
        cast("PlayerQueuesController", fake),
        QUEUE_ID,
        "session-1",
        only_while_playing=only_while_playing,
    )

    assert fake.resume.await_count == (0 if only_while_playing else 1)


def test_a_lost_item_that_is_still_current_restarts() -> None:
    """The lost stream of a single item restarts while that item is the current one."""
    fake = _fake_controller()
    fake._queue_data[QUEUE_ID].queue.current_item.queue_item_id = "item-1"

    assert _restart(fake, only_while_playing=True, queue_item_id="item-1") is True

    fake._restart_lost_flow_stream.assert_called_once_with(
        QUEUE_ID, "session-1", only_while_playing=True, queue_item_id="item-1"
    )


async def test_a_restart_waiting_for_the_lock_rechecks_its_item() -> None:
    """A move to another item while the restart waited for the lock stops it."""
    fake = _fake_controller()
    queue = fake._queue_data[QUEUE_ID].queue
    queue.current_item.queue_item_id = "item-1"

    @contextlib.asynccontextmanager
    async def _playback_lock(_queue_id: str) -> AsyncIterator[None]:
        # the player reported another item of the same session while the restart waited
        queue.current_item.queue_item_id = "item-2"
        yield

    fake.mass.players.get_group_and_player_lock = _playback_lock
    fake.resume = AsyncMock()

    await PlayerQueuesController._restart_lost_flow_stream(
        cast("PlayerQueuesController", fake),
        QUEUE_ID,
        "session-1",
        only_while_playing=True,
        queue_item_id="item-1",
    )

    fake.resume.assert_not_awaited()
