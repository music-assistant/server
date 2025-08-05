"""Client module for interacting with the NicoNico API."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING

from niconico import NicoNico
from niconico.exceptions import LoginFailureError  # Import LoginFailureError

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.niconico.adapters import (
    NiconicoAuthAdapter,
    NiconicoMylistAdapter,
    NiconicoSearchAdapter,
    NiconicoSeriesAdapter,
    NicoNicoUserAdapter,
    NiconicoVideoAdapter,
)
from music_assistant.providers.niconico.constants import ApiPriority

if TYPE_CHECKING:
    from music_assistant.providers.niconico.config import NiconicoConfig


class NicoNicoMusicAssistantAdapter:
    """Bridge NicoNico API and MusicAssistant."""

    def __init__(self, provider: MusicProvider, niconico_config: NiconicoConfig) -> None:
        """Initialize adapter with provider and config."""
        self.provider = provider
        self.niconico_config = niconico_config
        self.mass = provider.mass
        self.niconico_py_client = NicoNico()
        self.niconico_api_throttler = ThrottlerManager(rate_limit=1, period=0)
        # Low priority throttler for background tag updates (slower rate)
        self.niconico_api_throttler_low_priority = ThrottlerManager(rate_limit=1, period=1)
        self.logger = provider.logger.getChild("NicoNicoMusicAssistantAdapter")
        self.auth = NiconicoAuthAdapter(self)
        self.video = NiconicoVideoAdapter(self)
        self.series = NiconicoSeriesAdapter(self)
        self.mylist = NiconicoMylistAdapter(self)
        self.search = NiconicoSearchAdapter(self)
        self.user = NicoNicoUserAdapter(self)

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
        else:  # ApiPriority.LOW
            throttler = self.niconico_api_throttler_low_priority

        try:
            async with throttler.acquire():
                return await asyncio.to_thread(func, *args, **kwargs)
        except Exception as err:
            # Get caller information from stack
            frame = inspect.currentframe()
            caller_info = "unknown"
            operation = func.__name__ if hasattr(func, "__name__") else "unknown_function"

            try:
                # Walk up the stack to find the actual caller (skip this method and throttler)
                caller_frame = None
                if frame and frame.f_back and frame.f_back.f_back:
                    caller_frame = frame.f_back.f_back  # Skip current frame and acquire context

                if caller_frame:
                    caller_filename = caller_frame.f_code.co_filename
                    caller_function = caller_frame.f_code.co_name
                    caller_line = caller_frame.f_lineno
                    # Extract just the filename without full path for cleaner logs
                    filename = (
                        caller_filename.split("/")[-1]
                        if "/" in caller_filename
                        else caller_filename
                    )
                    caller_info = f"{filename}:{caller_function}:{caller_line}"
            except Exception:
                # Fallback if stack inspection fails
                caller_info = "stack_inspection_failed"
            finally:
                del frame  # Prevent reference cycles

            if isinstance(err, LoginFailureError):
                self.logger.warning(
                    "Authentication required for %s called from %s: %s", operation, caller_info, err
                )
            elif isinstance(err, (ConnectionError, TimeoutError)):
                self.logger.warning(
                    "Network error %s called from %s: %s", operation, caller_info, err
                )
            else:
                self.logger.warning("Error %s called from %s: %s", operation, caller_info, err)
            return None
