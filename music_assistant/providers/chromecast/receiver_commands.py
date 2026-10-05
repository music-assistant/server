"""Inbound custom-namespace messages from the Music Assistant Cast receiver app."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pychromecast.controllers import BaseController

from .constants import DASHBOARD_NAMESPACE

if TYPE_CHECKING:
    from collections.abc import Callable

    from pychromecast.generated.cast_channel_pb2 import CastMessage

PLAYER_COMMANDS = ("next", "previous")


class MassCastCommandController(BaseController):
    """Handles playback commands the Music Assistant Cast receiver app forwards."""

    def __init__(self, on_command: Callable[[str], None]) -> None:
        """
        Initialize the controller.

        :param on_command: Called with "next" or "previous" when the device's own
            UI (Google Home app, touch controls, remote) asks for a queue jump.
        """
        super().__init__(DASHBOARD_NAMESPACE)
        self._on_command = on_command

    def receive_message(self, _message: CastMessage, data: dict[str, Any]) -> bool:
        """
        Handle an incoming message on the Music Assistant Cast namespace.

        :param _message: The raw Cast protocol message.
        :param data: The parsed JSON payload.
        """
        if data.get("type") != "player_command":
            return False
        command = data.get("command")
        if command not in PLAYER_COMMANDS:
            return False
        self._on_command(command)
        return True
