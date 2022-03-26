"""Custom errors and exceptions."""


class MusicAssistantError(Exception):
    """Custom Exception for all errors."""


class ProviderUnavailableError(MusicAssistantError):
    """Error raised when trying to access mediaitem of unavailable provider."""


class MediaNotFoundError(MusicAssistantError):
    """Error raised when trying to access non existing media item."""


class InvalidDataError(MusicAssistantError):
    """Error raised when an object has invalid data."""
