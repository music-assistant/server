"""
Tests for the inbound half of the Music Assistant Cast namespace.

The receiver app forwards the device's own next/previous button presses as
custom messages (see https://github.com/music-assistant/support/issues/2969);
this controller turns them back into MA queue commands.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from music_assistant.providers.chromecast.constants import DASHBOARD_NAMESPACE
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
