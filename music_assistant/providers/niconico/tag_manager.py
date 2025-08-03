"""Tag manager for NicoNico provider with async caching and deduplication."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant.constants import CACHE_CATEGORY_MUSIC_PROVIDER_ITEM
from music_assistant.providers.niconico.constants import ApiPriority
from music_assistant.providers.niconico.helpers import log_verbose, log_verbose_operation

if TYPE_CHECKING:
    from music_assistant.models.music_provider import MusicProvider
    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class TagManager:
    """Manages video tag caching and retrieval for NicoNico provider."""

    def __init__(
        self, provider: MusicProvider, niconico_adapter: NicoNicoMusicAssistantAdapter
    ) -> None:
        """Initialize TagManager with provider and adapter references."""
        self.provider = provider
        self.niconico_adapter = niconico_adapter
        self.logger = provider.logger.getChild("tag_manager")

        # Track ongoing fetch operations to avoid duplicates
        self._fetch_tasks: dict[str, asyncio.Task[list[str]]] = {}

        # Cache configuration
        self._cache_category = CACHE_CATEGORY_MUSIC_PROVIDER_ITEM
        self._cache_expiration = 86400 * 7  # 7 days

        # Start periodic cleanup task
        self._cleanup_timer_id = "niconico_tag_cleanup"
        self._start_cleanup_timer()

    def trigger_update(self, video_id: str) -> None:
        """Trigger async tag update (fire-and-forget) for the given video ID."""
        if not video_id:
            return

        # Don't start new fetch if one is already running
        if video_id in self._fetch_tasks and not self._fetch_tasks[video_id].done():
            return

        # Start async fetch and cache operation with priority handling
        task = self.provider.mass.create_task(self._fetch_and_cache_with_priority(video_id))
        self._fetch_tasks[video_id] = task

    async def get_tags(self, video_id: str, wait_if_fetching: bool = False) -> list[str]:
        """Get tags for video from cache or fetch if needed.

        Args:
            video_id: NicoNico video ID
            wait_if_fetching: If True, wait for ongoing fetch operation

        Returns:
            List of tag strings, empty list if not available
        """
        if not video_id:
            return []

        # Try to get from cache first
        cached_tags = await self._get_cached_tags(video_id)
        if cached_tags is not None:
            return cached_tags

        # Check if we're currently fetching this video's tags
        if video_id in self._fetch_tasks and not self._fetch_tasks[video_id].done():
            if wait_if_fetching:
                try:
                    return await self._fetch_tasks[video_id]
                except Exception as err:
                    self.logger.warning("Failed to wait for tag fetch %s: %s", video_id, err)
                    return []
            else:
                # Don't wait, return empty list for now
                return []

        # No cache and no ongoing fetch - start new fetch if wait_if_fetching is True
        if wait_if_fetching:
            try:
                return await self._fetch_and_cache(video_id, priority=ApiPriority.HIGH)
            except Exception as err:
                self.logger.warning("Failed to fetch tags for %s: %s", video_id, err)
                return []

        # Return empty list if we can't get tags immediately
        return []

    async def _fetch_and_cache_with_priority(self, video_id: str) -> list[str]:
        """Fetch and cache tags with priority handling - delay if cache exists."""
        # Check if we already have cached tags
        cached_tags = await self._get_cached_tags(video_id)

        if cached_tags is not None:
            # Tags exist in cache - schedule delayed background update
            log_verbose(self.logger, "Tags exist for %s, scheduling delayed update", video_id)
            self.provider.mass.create_task(
                self._fetch_and_cache(video_id, priority=ApiPriority.LOW)
            )
            return cached_tags

        # No cache data - fetch with normal priority
        return await self._fetch_and_cache(video_id, priority=ApiPriority.HIGH)

    async def _get_cached_tags(self, video_id: str) -> list[str] | None:
        """Get tags from cache if available and valid.

        Returns:
            List of tag strings if cached and valid, None if not cached or invalid
        """
        cache_key = f"tags_{video_id}"
        cached_tags = await self.provider.mass.cache.get(
            cache_key,
            category=self._cache_category,
            base_key=self.provider.lookup_key,
        )

        if cached_tags is not None:
            # Validate cache data
            if isinstance(cached_tags, list) and all(isinstance(tag, str) for tag in cached_tags):
                return cached_tags
            else:
                # Invalid cache data
                log_verbose(
                    self.logger, "Invalid cache data for %s, treating as uncached", video_id
                )

        return None

    async def _fetch_and_cache(self, video_id: str, priority: ApiPriority) -> list[str]:
        """Fetch tags from API and cache them.

        Args:
            video_id: NicoNico video ID
            priority: API priority level (HIGH or LOW)

        Returns:
            List of tag strings, empty list on failure
        """
        try:
            # Fetch tags using the video adapter with specified priority
            tags_data = await self.niconico_adapter.video.get_video_tags(
                video_id, priority=priority
            )

            # Cache the tags
            cache_key = f"tags_{video_id}"
            await self.provider.mass.cache.set(
                cache_key,
                tags_data,
                expiration=self._cache_expiration,
                category=self._cache_category,
                base_key=self.provider.lookup_key,
            )

            log_verbose_operation(
                self.logger,
                "cached_tags",
                video_id,
                count=len(tags_data),
                priority=priority.value,
            )
            return tags_data

        except Exception as err:
            self.logger.warning(
                "Failed to fetch and cache tags (%s priority) for %s: %s",
                priority.value,
                video_id,
                err,
            )
            return []
        finally:
            # Remove from fetch_tasks when done
            self._fetch_tasks.pop(video_id, None)

    def _start_cleanup_timer(self) -> None:
        """Start periodic cleanup of completed tasks."""

        def run_cleanup() -> None:
            """Run cleanup and reschedule."""
            self._cleanup_completed_tasks()
            # Reschedule next cleanup in 5 minutes
            self.provider.mass.call_later(
                300.0,  # 5 minutes
                run_cleanup,
                task_id=self._cleanup_timer_id,
            )

        # Schedule first cleanup
        self.provider.mass.call_later(
            300.0,  # 5 minutes
            run_cleanup,
            task_id=self._cleanup_timer_id,
        )

    def _cleanup_completed_tasks(self) -> None:
        """Clean up completed fetch tasks to prevent memory leaks."""
        completed_tasks = [video_id for video_id, task in self._fetch_tasks.items() if task.done()]
        for video_id in completed_tasks:
            self._fetch_tasks.pop(video_id, None)
        if completed_tasks:
            log_verbose(self.logger, "Cleaned up %d completed tasks", len(completed_tasks))

    def stop(self) -> None:
        """Stop the TagManager and cleanup resources."""
        # Cancel cleanup timer
        self.provider.mass.cancel_timer(self._cleanup_timer_id)

        # Cancel all pending fetch tasks
        for task in self._fetch_tasks.values():
            if not task.done():
                task.cancel()
        self._fetch_tasks.clear()
