"""Custom errors and exceptions."""


class MusicAssistantError(Exception):
    """Custom Exception for all errors."""


class ProviderUnavailableError(MusicAssistantError):
    """Error raised when trying to access mediaitem of unavailable provider."""


class MediaNotFoundError(MusicAssistantError):
    """Error raised when trying to access non existing media item."""


class InvalidDataError(MusicAssistantError):
    """Error raised when an object has invalid data."""


class AlreadyRegisteredError(MusicAssistantError):
    """Error raised when a duplicate music provider or player is registered."""


class SetupFailedError(MusicAssistantError):
    """Error raised when setup of a provider or player failed."""


class LoginFailed(MusicAssistantError):
    """Error raised when a login failed."""


class AudioError(MusicAssistantError):
    """Error raised when an issue arrised when processing audio."""


class QueueEmpty(MusicAssistantError):
    """Error raised when trying to start queue stream while queue is empty."""
