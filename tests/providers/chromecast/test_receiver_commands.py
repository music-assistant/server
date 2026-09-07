"""
Tests for the inbound half of the Music Assistant Cast namespace.

The receiver app forwards the device's own next/previous button presses as
custom messages (see https://github.com/music-assistant/support/issues/2969);
this controller turns them back into MA queue commands.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import PlaybackState

from music_assistant.providers.chromecast.constants import DASHBOARD_NAMESPACE
from music_assistant.providers.chromecast.player import ChromecastPlayer
from music_assistant.providers.chromecast.receiver_commands import MassCastCommandController


def test_controller_uses_the_mass_cast_namespace() -> None:
    """The controller listens on the same namespace the receiver replies on."""
    controller = MassCastCommandController(MagicMock())

    assert controller.namespace == DASHBOARD_NAMESPACE


@pytest.mark.parametrize("command", ["next", "previous"])
def test_player_command_is_forwarded(command: str) -> None:
    """A player_command message invokes the callback and is marked handled."""
    on_command = MagicMock()
    controller = MassCastCommandController(on_command)

    handled = controller.receive_message(
        MagicMock(), {"type": "player_command", "command": command}
    )

    assert handled is True
    on_command.assert_called_once_with(command)


@pytest.mark.parametrize(
    "data",
    [
        {"type": "receiver_status", "connected": True},
        {"type": "player_command", "command": "shuffle"},
        {"type": "player_command"},
        {},
    ],
)
def test_unhandled_messages_are_ignored(data: dict[str, object]) -> None:
    """Other namespace traffic is left for other handlers and never dispatched."""
    on_command = MagicMock()
    controller = MassCastCommandController(on_command)

    assert controller.receive_message(MagicMock(), data) is False
    on_command.assert_not_called()


### Dispatch into the queue controller


def _fake_cast(
    *,
    queue_id: str | None = "cast_queue",
    queue_active: bool = True,
    queue_state: PlaybackState = PlaybackState.PLAYING,
) -> MagicMock:
    """Build a MagicMock Cast whose call_soon_threadsafe hop runs the callback inline."""
    fake = MagicMock()
    fake.display_name = "Fake Cast"
    fake.mass.closing = False
    # call_soon_threadsafe runs the callback inline so the dispatch is observable
    fake.mass.loop.call_soon_threadsafe = MagicMock(side_effect=lambda func, *args: func(*args))
    if queue_id is None:
        fake.mass.players.get_active_queue.return_value = None
    else:
        queue = MagicMock()
        queue.queue_id = queue_id
        queue.active = queue_active
        queue.state = queue_state
        fake.mass.players.get_active_queue.return_value = queue
    return fake


def _dispatch(fake: MagicMock, command: str) -> None:
    ChromecastPlayer._handle_receiver_command(cast("ChromecastPlayer", fake), command)


def test_next_command_targets_the_active_queue() -> None:
    """Next is dispatched to the queue that get_active_queue resolves for this player."""
    fake = _fake_cast(queue_id="up_universal")

    _dispatch(fake, "next")

    fake.mass.loop.call_soon_threadsafe.assert_called_once()
    fake.mass.players.get_active_queue.assert_called_once_with(fake)
    fake.mass.create_task.assert_called_once()
    fake.mass.player_queues.next.assert_called_once_with("up_universal")


def test_previous_command_targets_the_active_queue() -> None:
    """Previous is routed to the same resolved queue id."""
    fake = _fake_cast()

    _dispatch(fake, "previous")

    fake.mass.loop.call_soon_threadsafe.assert_called_once()
    fake.mass.player_queues.previous.assert_called_once_with("cast_queue")


def test_no_active_queue_is_ignored() -> None:
    """A command without any resolvable queue (dashboard-only session) is dropped."""
    fake = _fake_cast(queue_id=None)

    _dispatch(fake, "next")

    fake.mass.player_queues.next.assert_not_called()
    fake.mass.create_task.assert_not_called()


def test_inactive_queue_is_ignored() -> None:
    """A command whose resolved queue is not active is dropped, not dispatched."""
    fake = _fake_cast(queue_active=False)

    _dispatch(fake, "next")

    fake.mass.player_queues.next.assert_not_called()
    fake.mass.create_task.assert_not_called()


def test_idle_queue_is_ignored() -> None:
    """An idle queue still reports active=True; a press must not start playback."""
    fake = _fake_cast(queue_state=PlaybackState.IDLE)

    _dispatch(fake, "next")

    fake.mass.player_queues.next.assert_not_called()
    fake.mass.create_task.assert_not_called()


def test_commands_are_ignored_during_shutdown() -> None:
    """A press racing MusicAssistant.stop() must not schedule a new queue task."""
    fake = _fake_cast()
    fake.mass.closing = True

    _dispatch(fake, "next")

    fake.mass.loop.call_soon_threadsafe.assert_not_called()
    fake.mass.create_task.assert_not_called()


def test_paused_queue_is_dispatched() -> None:
    """Next while paused is a legitimate command and goes through."""
    fake = _fake_cast(queue_state=PlaybackState.PAUSED)

    _dispatch(fake, "next")

    fake.mass.player_queues.next.assert_called_once_with("cast_queue")
