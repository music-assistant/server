"""Client module for interacting with the NicoNico API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from niconico import NicoNico

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

T = TypeVar("T")
P = ParamSpec("P")


class NicoNicoMusicAssistantAdapter:
    """Bridge NicoNico API and MusicAssistant."""

    def __init__(self, provider: MusicProvider) -> None:
        """Initialize adapter with provider."""
        self.provider = provider
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

    async def call_with_throttler(
        self,
        func: Callable[P, T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Call function with API throttling."""
        return await self.call_with_throttler_with_priority(ApiPriority.HIGH, func, *args, **kwargs)

    async def call_with_throttler_with_priority(
        self,
        priority: ApiPriority,
        func: Callable[P, T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Call function with API throttling (unified method with priority support)."""
        if priority == ApiPriority.HIGH:
            throttler = self.niconico_api_throttler
            self.provider.logger.debug("Calling %s with high priority throttler", func.__name__)
        else:  # ApiPriority.LOW
            throttler = self.niconico_api_throttler_low_priority

        async with throttler.acquire():
            return await asyncio.to_thread(func, *args, **kwargs)
