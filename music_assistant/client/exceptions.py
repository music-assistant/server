"""Client-specific Exceptions for Music Assistant."""

from __future__ import annotations


class MusicAssistantClientException(Exception):
    """Generic MusicAssistant exception."""


class TransportError(MusicAssistantClientException):
    """Exception raised to represent transport errors."""

    def __init__(self, message: str, error: Exception | None = None) -> None:
        """Initialize a transport error."""
        super().__init__(message)
        self.error = error


class ConnectionClosed(TransportError):
    """Exception raised when the connection is closed."""


class CannotConnect(TransportError):
    """Exception raised when failed to connect the client."""

    def __init__(self, error: Exception) -> None:
        """Initialize a cannot connect error."""
        super().__init__(f"{error}", error)


class ConnectionFailed(TransportError):
    """Exception raised when an established connection fails."""

    def __init__(self, error: Exception | None = None) -> None:
        """Initialize a connection failed error."""
        if error is None:
            super().__init__("Connection failed.")
            return
        super().__init__(f"{error}", error)


class NotConnected(MusicAssistantClientException):
    """Exception raised when not connected to client."""


class InvalidState(MusicAssistantClientException):
    """Exception raised when data gets in invalid state."""


class InvalidMessage(MusicAssistantClientException):
    """Exception raised when an invalid message is received."""


class InvalidServerVersion(MusicAssistantClientException):
    """Exception raised when connected to server with incompatible version."""
