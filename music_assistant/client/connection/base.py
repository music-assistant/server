""""Connection/transport to exchange messages to/from a remote Music Assistant Server."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Connection(ABC):
    """Base model for a connection to a Music Assistant Server."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Return if we're currently connected."""

    @abstractmethod
    async def connect(self) -> dict[str, Any]:
        """Connect to the websocket server and return the first message (server info)."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Handle disconnect."""

    @abstractmethod
    async def receive_message(self) -> dict[str, Any]:
        """Receive the next message from the server (or raise on error)."""

    @abstractmethod
    async def send_message(self, message: dict[str, Any]) -> None:
        """
        Send a CommandMessage to the server.

        Raises NotConnected if client not connected.
        """
