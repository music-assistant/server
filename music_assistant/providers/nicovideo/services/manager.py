"""
Manager service for niconico API integration with MusicAssistant.

Services Layer: API integration and data transformation coordination
- Coordinates API calls through niconico.py adapter
- Manages authentication and session management
- Handles API rate limiting and throttling
- Delegates data transformation to converters
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING

from niconico import NicoNico
from niconico.exceptions import LoginFailureError, LoginRequiredError, PremiumRequiredError
from pydantic import ValidationError

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.nicovideo.converters.manager import (
    NicovideoConverterManager,
)
from music_assistant.providers.nicovideo.services.auth import NicovideoAuthService
from music_assistant.providers.nicovideo.services.mylist import NicovideoMylistService
from music_assistant.providers.nicovideo.services.search import NicovideoSearchService
from music_assistant.providers.nicovideo.services.series import NicovideoSeriesService
from music_assistant.providers.nicovideo.services.user import NicovideoUserService
from music_assistant.providers.nicovideo.services.video import NicovideoVideoService

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.config import NicovideoConfig


class NicovideoServiceManager:
    """Central manager for all niconico services and MusicAssistant integration."""

    def __init__(self, provider: MusicProvider, nicovideo_config: NicovideoConfig) -> None:
        """Initialize service manager with provider and config."""
        self.provider = provider
        self.nicovideo_config = nicovideo_config
        self.mass = provider.mass
        self.reset_niconico_py_client()

        self.niconico_api_throttler = ThrottlerManager(rate_limit=5, period=1)

        self.logger = provider.logger

        # Initialize services for different functionality
        self.auth = NicovideoAuthService(self)
        self.video = NicovideoVideoService(self)
        self.series = NicovideoSeriesService(self)
        self.mylist = NicovideoMylistService(self)
        self.search = NicovideoSearchService(self)
        self.user = NicovideoUserService(self)

        # Initialize converter
        self.converter_manager = NicovideoConverterManager(provider, self.logger)

    def reset_niconico_py_client(self) -> None:
        """Reset the niconico.py client instance."""
        self.niconico_py_client = NicoNico()

    def _extract_caller_info(self) -> str:
        """Extract best-effort caller info file:function:line for diagnostics."""
        frame = inspect.currentframe()
        caller_info = "unknown"
        try:
            caller_frame = None
            if frame and frame.f_back and frame.f_back.f_back:
                caller_frame = frame.f_back.f_back  # Skip this method and acquire context
            if caller_frame:
                caller_filename = caller_frame.f_code.co_filename
                caller_function = caller_frame.f_code.co_name
                caller_line = caller_frame.f_lineno
                filename = caller_filename.rsplit("/", 1)[-1]
                caller_info = f"{filename}:{caller_function}:{caller_line}"
        except Exception:
            caller_info = "stack_inspection_failed"
        finally:
            del frame  # Prevent reference cycles
        return caller_info

    def _log_call_exception(self, operation: str, err: Exception) -> None:
        """Log exceptions with classification and caller info."""
        caller_info = self._extract_caller_info()
        if isinstance(err, LoginRequiredError):
            self.logger.debug(
                "Authentication required for %s called from %s: %s", operation, caller_info, err
            )
        elif isinstance(err, PremiumRequiredError):
            self.logger.warning(
                "Premium account required for %s called from %s: %s", operation, caller_info, err
            )
        elif isinstance(err, LoginFailureError):
            self.logger.warning(
                "Login failed for %s called from %s: %s", operation, caller_info, err
            )
        elif isinstance(err, (ConnectionError, TimeoutError)):
            self.logger.warning("Network error %s called from %s: %s", operation, caller_info, err)
        elif isinstance(err, ValidationError):
            try:
                detailed_errors = err.errors()
                self.logger.warning(
                    "Validation error %s called from %s: %s\nDetailed errors: %s",
                    operation,
                    caller_info,
                    err,
                    detailed_errors,
                )
            except Exception:
                self.logger.warning("Error %s called from %s: %s", operation, caller_info, err)
        else:
            self.logger.warning("Error %s called from %s: %s", operation, caller_info, err)

    async def _call_with_throttler[T, **P](
        self,
        func: Callable[P, T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T | None:
        """Call function with API throttling."""
        try:
            async with self.niconico_api_throttler.acquire():
                return await asyncio.to_thread(func, *args, **kwargs)
        except Exception as err:
            operation = func.__name__ if hasattr(func, "__name__") else "unknown_function"
            self._log_call_exception(operation, err)
            return None
