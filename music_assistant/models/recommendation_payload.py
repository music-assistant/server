"""Cached-payload mixin for providers whose recommendations come from one bulk payload."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

from music_assistant_models.media_items import RecommendationFolder
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache

if TYPE_CHECKING:
    from logging import Logger

    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

    from music_assistant import MusicAssistant

    class _RecommendationPayloadHost(Protocol):
        """Requirements a provider must fulfil to use RecommendationPayloadMixin."""

        mass: MusicAssistant
        logger: Logger

        @property
        def domain(self) -> str: ...

        @property
        def instance_id(self) -> str: ...

        async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
            """Fetch and parse the full recommendations payload (folders WITH items)."""
            ...

    _MixinBase = _RecommendationPayloadHost
else:
    _MixinBase = object


class RecommendationPayloadMixin(_MixinBase):
    """
    Mixin serving recommendation rows and items from a single (cached) bulk payload.

    The provider implements _fetch_recommendation_payload() with its existing bulk
    backend fetch+parse; the mixin derives both the fast rows call and the per-row
    items call from that payload, with persistent SWR caching and deduplication of
    concurrent fetches.
    """

    _recommendation_payload_task: asyncio.Task[list[RecommendationFolder]] | None = None

    async def _recommendation_rows_from_payload(self) -> list[RecommendationFolder]:
        """Return all payload folders as rows, without items."""
        return [
            RecommendationFolder(
                item_id=folder.item_id,
                provider=folder.provider,
                name=folder.name,
                translation_key=folder.translation_key,
                icon=folder.icon,
                subtitle=folder.subtitle,
                enabled_by_default=folder.enabled_by_default,
            )
            for folder in await self._recommendation_payload()
        ]

    async def _recommendation_items_from_payload(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return the items of the payload folder matching the given item_id.

        :param item_id: The item_id of the recommendation folder (empty result if unknown).
        """
        for folder in await self._recommendation_payload():
            if folder.item_id == item_id:
                return folder.items
        return UniqueList()

    async def _recommendation_payload(self) -> list[RecommendationFolder]:
        """Return the full recommendations payload, cached and deduplicated."""
        task = self._recommendation_payload_task
        if task is None or task.done():
            # single-flight: share one in-flight fetch between concurrent callers,
            # as the cache decorator does not dedupe concurrent cold misses
            task = asyncio.create_task(self._cached_recommendation_payload())
            self._recommendation_payload_task = task
        # shield: a timed-out caller must not cancel the shared fetch out from under
        # the other waiters (and the fetch must still complete to warm the cache)
        return await asyncio.shield(task)

    @use_cache(
        expiration=3600, persistent=True, allow_expired_cache=True, base_class=RecommendationFolder
    )
    async def _cached_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch the recommendations payload through the persistent cache."""
        return await self._fetch_recommendation_payload()
