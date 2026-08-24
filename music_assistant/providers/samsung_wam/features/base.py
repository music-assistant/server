"""Base classes for WAM features."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from logging import Logger
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import PlayerCommandFailed
from pywam.lib.exceptions import ApiCallTimeoutError, PywamError

from music_assistant.providers.samsung_wam.consts import (
    COMMAND_RETRY_ATTEMPTS,
    COMMAND_RETRY_BACKOFF,
)

if TYPE_CHECKING:
    from pywam.speaker import Speaker

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.samsung_wam.player import WamPlayer
    from music_assistant.providers.samsung_wam.provider import SamsungWamProvider


class WamProviderFeatureBase:
    """Base class for provider feature handlers."""

    def __init__(self, provider: SamsungWamProvider) -> None:
        """
        Initialize the feature base with the parent provider instance.

        :param provider: The SamsungWamProvider instance.
        """
        self.provider = provider

    @property
    def mass(self) -> MusicAssistant:
        """Return the MusicAssistant instance."""
        return self.provider.mass

    @property
    def logger(self) -> Logger:
        """Return the provider's dedicated logger."""
        return self.provider.logger

    @property
    def players(self) -> list[WamPlayer]:
        """Return all registered WAM players."""
        return self.provider.get_players()


class WamPlayerFeatureBase:
    """Base class for player feature handlers."""

    def __init__(self, player: WamPlayer) -> None:
        """
        Initialize the feature base with the parent player instance.

        :param player: The WamPlayer instance.
        """
        self.player = player

    @property
    def mass(self) -> MusicAssistant:
        """Return the MusicAssistant instance."""
        return self.player.mass

    @property
    def speaker(self) -> Speaker:
        """Return the pywam Speaker instance."""
        return self.player.speaker

    @property
    def logger(self) -> Logger:
        """Return the player's dedicated logger."""
        return self.player.logger


def retry_command(
    attempts: int = COMMAND_RETRY_ATTEMPTS,
    initial_backoff: float = COMMAND_RETRY_BACKOFF,
) -> Callable[[Callable[..., Coroutine[Any, Any, Any]]], Callable[..., Coroutine[Any, Any, Any]]]:
    """
    Decorate a player command to retry on temporary failures.

    :param attempts: Total number of attempts to make.
    :param initial_backoff: Initial wait time in seconds before retrying.
    """

    def decorator(
        func: Callable[..., Coroutine[Any, Any, Any]],
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        @wraps(func)
        async def wrapper(self: WamPlayerFeatureBase, *args: Any, **kwargs: Any) -> Any:
            backoff_delay = initial_backoff
            last_error: Exception | None = None

            for attempt in range(attempts):
                try:
                    await self.player.state_sync.ensure_speaker_connected()
                    result = await func(self, *args, **kwargs)

                    if attempt > 0:
                        self.logger.debug(
                            "Command '%s' recovered on attempt %s/%s",
                            func.__name__,
                            attempt + 1,
                            attempts,
                        )
                    return result

                except (TimeoutError, PlayerCommandFailed, PywamError) as err:
                    last_error = err
                    if attempt < attempts - 1:
                        self.logger.debug(
                            "Command '%s' failed on attempt %s/%s, retrying in %.1fs",
                            func.__name__,
                            attempt + 1,
                            attempts,
                            backoff_delay,
                        )
                        await asyncio.sleep(backoff_delay)
                        backoff_delay *= 2
                    else:
                        self.logger.debug(
                            "Command '%s' failed on final attempt %s/%s",
                            func.__name__,
                            attempt + 1,
                            attempts,
                        )

            self.logger.warning("Command '%s' failed after %s attempts", func.__name__, attempts)
            raise PlayerCommandFailed(
                f"Command '{func.__name__}' failed after {attempts} attempts"
            ) from last_error

        return wrapper

    return decorator


def handle_pywam_errors(
    func: Callable[..., Coroutine[Any, Any, Any]],
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Decorate a command to translate pywam exceptions into player command errors."""

    @wraps(func)
    async def wrapper(self: WamPlayerFeatureBase, *args: Any, **kwargs: Any) -> Any:
        try:
            return await func(self, *args, **kwargs)
        except ConnectionError as err:
            self.logger.warning("Command failed with a connection error")
            raise PlayerCommandFailed("Connection lost") from err
        except (ApiCallTimeoutError, TimeoutError, PywamError) as err:
            raise PlayerCommandFailed(str(err)) from err

    return wrapper
