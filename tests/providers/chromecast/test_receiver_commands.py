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
    player_id: str = "cast_id",
    protocol_parent_id: str | None = None,
    active_cast_group: str | None = None,
) -> MagicMock:
    """Build a MagicMock Cast whose call_soon_threadsafe hop runs the callback inline."""
    fake = MagicMock()
    fake.player_id = player_id
    fake.protocol_parent_id = protocol_parent_id
    fake.active_cast_group = active_cast_group
    # call_soon_threadsafe runs the callback inline so the dispatch is observable
    fake.mass.loop.call_soon_threadsafe = MagicMock(side_effect=lambda func, *args: func(*args))
    # deliberately active: the resolved queue must exist and be active for a
    # command to reach player_queues.next/previous, see test_inactive_queue_is_ignored
    fake.mass.player_queues.get.return_value.active = True
    return fake


def _dispatch(fake: MagicMock, command: str) -> None:
    ChromecastPlayer._handle_receiver_command(cast("ChromecastPlayer", fake), command)


def test_next_command_targets_the_queue_owner() -> None:
    """A Cast wrapped by a universal player forwards next to the parent's queue."""
    fake = _fake_cast(protocol_parent_id="up_universal")

    _dispatch(fake, "next")

    fake.mass.loop.call_soon_threadsafe.assert_called_once()
    fake.mass.create_task.assert_called_once()
    fake.mass.player_queues.next.assert_called_once_with("up_universal")


def test_previous_command_targets_the_queue_owner() -> None:
    """Previous is routed to the same resolved queue id."""
    fake = _fake_cast()

    _dispatch(fake, "previous")

    fake.mass.loop.call_soon_threadsafe.assert_called_once()
    fake.mass.player_queues.previous.assert_called_once_with("cast_id")


def test_command_prefers_the_active_cast_group() -> None:
    """A Cast group child mirrors the group's queue, taking precedence."""
    fake = _fake_cast(protocol_parent_id="up_universal", active_cast_group="cast_group")

    _dispatch(fake, "next")

    fake.mass.loop.call_soon_threadsafe.assert_called_once()
    fake.mass.player_queues.next.assert_called_once_with("cast_group")


def test_inactive_queue_is_ignored() -> None:
    """A command on a dashboard-only session (no active MA queue) is dropped, not dispatched."""
    fake = _fake_cast()
    fake.mass.player_queues.get.return_value.active = False

    _dispatch(fake, "next")

    fake.mass.player_queues.next.assert_not_called()
    fake.mass.create_task.assert_not_called()
