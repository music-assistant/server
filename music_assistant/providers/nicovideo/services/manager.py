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
from niconico.exceptions import LoginFailureError
from pydantic import ValidationError

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.nicovideo.constants import ApiPriority
from music_assistant.providers.nicovideo.converters.manager import (
    NicovideoConverterManager,
)
from music_assistant.providers.nicovideo.helpers import log_verbose
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

        self.niconico_api_throttler = ThrottlerManager(rate_limit=1, period=0)
        # Low priority throttler for background tag updates (slower rate)
        self.niconico_api_throttler_low_priority = ThrottlerManager(rate_limit=1, period=0.3)

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

    def _safe_summarize(self, value: object) -> str:
        """Summarize a value safely for logs (mask secrets, truncate long)."""
        try:
            s = str(value)
        except Exception:
            return "<unprintable>"
        low = s.lower()
        if any(k in low for k in ("cookie", "token", "session", "password")):
            return "<masked>"
        return (s[:200] + "…") if len(s) > 200 else s

    def _summarize_call_args(
        self, args: tuple[object, ...], kwargs: dict[str, object]
    ) -> tuple[str, str]:
        """Create safe summaries for positional and keyword args."""
        try:
            arg_summary = ", ".join(self._safe_summarize(a) for a in args)
        except Exception:
            arg_summary = "<args>"
        try:
            kw_summary = ", ".join(f"{k}={self._safe_summarize(v)}" for k, v in kwargs.items())
        except Exception:
            kw_summary = "<kwargs>"
        return arg_summary, kw_summary

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
        if isinstance(err, LoginFailureError):
            self.logger.warning(
                "Authentication required for %s called from %s: %s", operation, caller_info, err
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
        return await self._call_with_throttler_with_priority(
            ApiPriority.HIGH, func, *args, **kwargs
        )

    async def _call_with_throttler_with_priority[T, **P](
        self,
        priority: ApiPriority,
        func: Callable[P, T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T | None:
        """Call function with API throttling (unified method with priority support)."""
        if priority == ApiPriority.HIGH:
            throttler = self.niconico_api_throttler
            throttler_name = "high_priority"
        else:  # ApiPriority.LOW
            throttler = self.niconico_api_throttler_low_priority
            throttler_name = "low_priority"

        operation = func.__name__ if hasattr(func, "__name__") else "unknown_function"
        arg_summary, kw_summary = self._summarize_call_args(args, kwargs)
        log_verbose(
            self.logger,
            "Acquire %s throttler for %s(%s%s%s)",
            throttler_name,
            operation,
            arg_summary,
            ", " if arg_summary and kw_summary else "",
            kw_summary,
        )

        try:
            async with throttler.acquire():
                result = await asyncio.to_thread(func, *args, **kwargs)
                log_verbose(self.logger, "%s succeeded (priority=%s)", operation, throttler_name)
                return result
        except Exception as err:
            self._log_call_exception(operation, err)
            return None
