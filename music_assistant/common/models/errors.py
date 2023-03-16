"""Custom errors and exceptions."""


class MusicAssistantError(Exception):
    """Custom Exception for all errors."""

    error_code = 0


class ProviderUnavailableError(MusicAssistantError):
    """Error raised when trying to access mediaitem of unavailable provider."""

    error_code = 1


class MediaNotFoundError(MusicAssistantError):
    """Error raised when trying to access non existing media item."""

    error_code = 2


class InvalidDataError(MusicAssistantError):
    """Error raised when an object has invalid data."""

    error_code = 3


class AlreadyRegisteredError(MusicAssistantError):
    """Error raised when a duplicate music provider or player is registered."""

    error_code = 4


class SetupFailedError(MusicAssistantError):
    """Error raised when setup of a provider or player failed."""

    error_code = 5


class LoginFailed(MusicAssistantError):
    """Error raised when a login failed."""

    error_code = 6


class AudioError(MusicAssistantError):
    """Error raised when an issue arrised when processing audio."""

    error_code = 7


class QueueEmpty(MusicAssistantError):
    """Error raised when trying to start queue stream while queue is empty."""

    error_code = 8


class UnsupportedFeaturedException(MusicAssistantError):
    """Error raised when a feature is not supported."""

    error_code = 9


class PlayerUnavailableError(MusicAssistantError):
    """Error raised when trying to access non-existing or unavailable player."""

    error_code = 10


class PlayerCommandFailed(MusicAssistantError):
    """Error raised when a command to a player failed execution."""

    error_code = 11


class InvalidCommand(MusicAssistantError):
    """Error raised when an unknown command is requested on the API."""

    error_code = 12


class UnplayableMediaError(MusicAssistantError):
    """Error thrown when a MediaItem cannot be played properly."""

    error_code = 13


def error_code_to_exception(error_code: int) -> MusicAssistantError:
    """Return MusicAssistant Error (exception) from error_code."""
    match error_code:
        case 1:
            return ProviderUnavailableError
        case 2:
            return MediaNotFoundError
        case 3:
            return InvalidDataError
        case 4:
            return AlreadyRegisteredError
        case 5:
            return SetupFailedError
        case 6:
            return LoginFailed
        case 7:
            return AudioError
        case 8:
            return QueueEmpty
        case 9:
            return UnsupportedFeaturedException
        case 10:
            return PlayerUnavailableError
        case 11:
            return PlayerCommandFailed
        case 12:
            return InvalidCommand
        case 13:
            return UnplayableMediaError
        case _:
            return MusicAssistantError
