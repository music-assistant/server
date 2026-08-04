"""Cached-payload mixin for providers whose recommendations come from one bulk payload."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import TYPE_CHECKING, TypeVar, cast

from music_assistant_models.media_items import RecommendationFolder
from music_assistant_models.unique_list import UniqueList

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any

    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

    from music_assistant.helpers.json import SerializableType

    # type the mixin against the Provider base it composes with, so self.mass,
    # self.logger, instance_id and the unload() chain resolve without redeclaring
    # any (final) Provider members; at runtime the mixin stays a plain object base
    from music_assistant.models.provider import Provider as _MixinBase
else:
    _MixinBase = object

_T = TypeVar("_T")

# Key of the persistent payload cache entry (stored with provider=instance_id).
# Kept identical to the key the previous @use_cache-based implementation produced,
# so payloads persisted before the redesign still warm cold instances.
_PAYLOAD_CACHE_KEY = "_cached_recommendation_payload"


class RecommendationPayloadMixin(_MixinBase):
    """
    Mixin serving recommendation rows and items from a single (cached) bulk payload.

    The provider implements _fetch_recommendation_payload() with its existing bulk
    backend fetch+parse; the mixin derives both the fast rows call and the per-row
    items call from that payload.

    Caching works in two layers:
    - In memory: the last fetched payload is kept on the instance and served directly
      while it is younger than recommendation_payload_ttl (no cache-db round trip).
      A stale in-memory payload is served immediately while a single background
      refresh replaces it (stale-while-revalidate).
    - Persistent: every successful fetch is stored in mass.cache (persistent=True), so
      a cold instance (e.g. after a restart) warms from the cache db with at most one
      read; a stale persisted payload is likewise served while one refresh runs.

    Concurrent callers share one in-flight fetch (single-flight); the shared fetch is
    isolated from caller cancellation, so a timed-out caller cannot cancel it for the
    other waiters and its result still lands in memory and cache.

    All background work runs in tasks created via mass.create_task, so it is cancelled
    on server stop; the mixin's unload() additionally cancels any in-flight fetch or
    refresh when the provider itself is unloaded. For that override to be reachable,
    list the mixin before the provider base class, e.g.
    ``class MyProvider(RecommendationPayloadMixin, MusicProvider)``. A cancelled fetch
    does not poison later calls: the next call simply starts a fresh one.
    """

    recommendation_payload_ttl: int = 3600
    """Seconds a fetched payload is served as fresh. Subclasses may override."""

    _recommendation_payload_task: asyncio.Task[list[RecommendationFolder]] | None = None
    _recommendation_refresh_task: asyncio.Task[list[RecommendationFolder]] | None = None
    _recommendation_payload_memory: list[RecommendationFolder] | None = None
    _recommendation_payload_timestamp: float = 0.0
    _recommendation_payload_cache_checked: bool = False

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Cancels any in-flight payload fetch/refresh task and awaits its completion,
        so no payload work keeps running while the provider tears down, before
        continuing the regular provider unload chain.

        :param is_removed: True when the provider is removed from the configuration.
        """
        tasks = [
            task
            for task in (self._recommendation_payload_task, self._recommendation_refresh_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._recommendation_payload_task = None
        self._recommendation_refresh_task = None
        await super().unload(is_removed)

    async def _recommendation_rows_from_payload(self) -> list[RecommendationFolder]:
        """Return all payload folders as rows, without items."""
        # replace() keeps every payload field (image, is_playable, media_type, type, ...)
        # instead of enumerating them, so new folder fields survive the copy automatically
        return [
            dataclasses.replace(folder, items=UniqueList())
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
        if (payload := self._recommendation_payload_memory) is not None:
            if (
                time.monotonic() - self._recommendation_payload_timestamp
                < self.recommendation_payload_ttl
            ):
                return payload
            # stale: serve immediately while a (deduplicated) background refresh runs
            self._schedule_recommendation_refresh()
            return payload
        # cold instance: single-flight load from the persistent cache or the backend
        task = self._recommendation_payload_task
        if task is None or task.done():
            task = self._start_recommendation_task(
                self._cold_load_recommendation_payload(),
                task_id=f"recommendation_payload_fetch.{self.instance_id}",
            )
            self._recommendation_payload_task = task
        # wait for the shared load instead of awaiting it directly: a timed-out caller must
        # not cancel it out from under the other waiters (and the load must still complete
        # to warm memory + cache). asyncio.shield achieves the same, but as of Python 3.14 a
        # cancelled caller makes it report the load's exception through
        # loop.call_exception_handler, even when another caller already handled it.
        if not task.done():
            await asyncio.wait((task,))
        return task.result()

    async def _refresh_recommendation_payload(self) -> list[RecommendationFolder]:
        """
        Force-fetch a fresh payload and store it in memory and the payload cache.

        Unlike _recommendation_payload, this never serves cached data: use it when the
        cached payload is known to be outdated (e.g. after detecting rotated backend ids).
        Subsequent _recommendation_payload calls serve the refreshed payload.
        """
        # wait for the shared refresh rather than awaiting it: see _recommendation_payload
        task = self._schedule_recommendation_refresh()
        if not task.done():
            await asyncio.wait((task,))
        return task.result()

    def _schedule_recommendation_refresh(self) -> asyncio.Task[list[RecommendationFolder]]:
        """Return the in-flight refresh task, starting one if none is running."""
        task = self._recommendation_refresh_task
        if task is None or task.done():
            task = self._start_recommendation_task(
                self._fetch_and_store_recommendation_payload(),
                task_id=f"recommendation_payload_refresh.{self.instance_id}",
            )
            self._recommendation_refresh_task = task
        return task

    async def _cold_load_recommendation_payload(self) -> list[RecommendationFolder]:
        """Load the payload on a cold instance: persisted cache entry or backend fetch."""
        if not self._recommendation_payload_cache_checked:
            data, is_fresh, found = await self.mass.cache.get_with_freshness(
                _PAYLOAD_CACHE_KEY,
                provider=self.instance_id,
                base_class=RecommendationFolder,
                include_expired=True,
            )
            # set after the await: a cancelled read is retried on the next cold call
            self._recommendation_payload_cache_checked = True
            if found and data is not None:
                payload = cast("list[RecommendationFolder]", data)
                self._recommendation_payload_memory = payload
                if is_fresh:
                    self._recommendation_payload_timestamp = time.monotonic()
                else:
                    # expired entry: serve it now, refresh it in the background
                    self._recommendation_payload_timestamp = (
                        time.monotonic() - self.recommendation_payload_ttl
                    )
                    self._schedule_recommendation_refresh()
                return payload
        return await self._fetch_and_store_recommendation_payload()

    async def _fetch_and_store_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch the payload from the backend and store it in memory + persistent cache."""
        payload = await self._fetch_recommendation_payload()
        # memory first: warm calls are served from here without touching the cache db,
        # and a caller landing before the background store completes still gets a hit
        self._recommendation_payload_memory = payload
        self._recommendation_payload_timestamp = time.monotonic()
        self._start_recommendation_task(
            self.mass.cache.set(
                key=_PAYLOAD_CACHE_KEY,
                data=cast("SerializableType", payload),
                expiration=self.recommendation_payload_ttl,
                provider=self.instance_id,
                persistent=True,
                allow_expired_cache=True,
            )
        )
        return payload

    def _start_recommendation_task(
        self, coro: Coroutine[Any, Any, _T], task_id: str | None = None
    ) -> asyncio.Task[_T]:
        """Create a tracked background task that logs failures through the provider logger."""
        task = self.mass.create_task(coro, task_id=task_id)
        # always retrieve the task's exception: without a waiter (background refresh,
        # cancelled sole caller) it would otherwise surface as a raw loop-handler error
        task.add_done_callback(self._log_recommendation_task_failure)
        return task

    def _log_recommendation_task_failure(self, task: asyncio.Task[Any]) -> None:
        """Log (and thereby retrieve) the exception of a finished payload task, if any."""
        if task.cancelled():
            return
        if (err := task.exception()) is not None:
            self.logger.warning(
                "Recommendation payload task failed: %s",
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )

    async def _fetch_recommendation_payload(self) -> list[RecommendationFolder]:
        """Fetch and parse the full recommendations payload (folders WITH items)."""
        # the provider using this mixin supplies its bulk backend fetch here
        raise NotImplementedError
