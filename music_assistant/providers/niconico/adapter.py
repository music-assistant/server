"""Client module for interacting with the NicoNico API."""

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

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

T = TypeVar("T")


class NicoNicoMusicAssistantAdapter:
    """Bridge NicoNico API and MusicAssistant."""

    def __init__(self, provider: MusicProvider) -> None:
        """Initialize adapter with provider."""
        self.provider = provider
        self.mass = provider.mass
        self.niconico_py_client = NicoNico()
        self.niconico_api_throttler = ThrottlerManager(rate_limit=1, period=2)
        self.logger = provider.logger.getChild("NicoNicoMusicAssistantAdapter")
        self.auth = NiconicoAuthAdapter(self)
        self.video = NiconicoVideoAdapter(self)
        self.series = NiconicoSeriesAdapter(self)
        self.mylist = NiconicoMylistAdapter(self)
        self.search = NiconicoSearchAdapter(self)
        self.user = NicoNicoUserAdapter(self)

    async def call_with_throttler(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Call function with API throttling."""
        async with self.niconico_api_throttler.bypass():
            return await asyncio.to_thread(func, *args, **kwargs)
