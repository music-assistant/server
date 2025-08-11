"""Podcast-specific functionality for Spotify provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import MediaItemType, Podcast, PodcastEpisode

from .constants import (
    CACHE_CATEGORY_EPISODES,
    CACHE_CATEGORY_PODCASTS,
    CACHE_KEY_EPISODES_PREFIX,
    CACHE_KEY_PODCAST_PREFIX,
)
from .parsers import parse_podcast, parse_podcast_episode

if TYPE_CHECKING:
    from .provider import SpotifyProvider


class PodcastManager:
    """Handles podcast-specific functionality for Spotify provider."""

    def __init__(self, provider: SpotifyProvider):
        """Initialize the PodcastManager with a reference to the Spotify provider."""
        self.provider = provider
        self.logger = provider.logger
        self.mass = provider.mass

    @property
    def sync_played_status_enabled(self) -> bool:
        """Check if played status sync is enabled."""
        return self.provider.sync_played_status_enabled

    @property
    def played_threshold(self) -> float:
        """Get the played threshold percentage."""
        return self.provider.played_threshold

    # Cache-related methods
    async def _cache_get_podcast(self, podcast_id: str) -> dict[str, Any] | None:
        """Get cached podcast data."""
        return cast(
            "dict[str, Any] | None",
            await self.mass.cache.get(
                key=f"{CACHE_KEY_PODCAST_PREFIX}{podcast_id}",
                base_key=self.provider.lookup_key,
                category=CACHE_CATEGORY_PODCASTS,
                default=None,
            ),
        )

    async def _cache_set_podcast(self, podcast_id: str, podcast_data: dict[str, Any]) -> None:
        """Cache podcast data."""
        await self.mass.cache.set(
            key=f"{CACHE_KEY_PODCAST_PREFIX}{podcast_id}",
            base_key=self.provider.lookup_key,
            category=CACHE_CATEGORY_PODCASTS,
            data=podcast_data,
            expiration=60 * 60 * 24,  # 1 day
        )

    async def _cache_get_episodes(self, podcast_id: str) -> list[dict[str, Any]] | None:
        """Get cached episodes data."""
        return cast(
            "list[dict[str, Any]] | None",
            await self.mass.cache.get(
                key=f"{CACHE_KEY_EPISODES_PREFIX}{podcast_id}",
                base_key=self.provider.lookup_key,
                category=CACHE_CATEGORY_EPISODES,
                default=None,
            ),
        )

    async def _cache_set_episodes(
        self, podcast_id: str, episodes_data: list[dict[str, Any]]
    ) -> None:
        """Cache episodes data."""
        await self.mass.cache.set(
            key=f"{CACHE_KEY_EPISODES_PREFIX}{podcast_id}",
            base_key=self.provider.lookup_key,
            category=CACHE_CATEGORY_EPISODES,
            data=episodes_data,
            expiration=60 * 60 * 12,  # 12 hours for episodes (more dynamic content)
        )

    async def _cache_invalidate_podcast(self, podcast_id: str) -> None:
        """Invalidate podcast and episodes cache."""
        await self.mass.cache.delete(
            key=f"{CACHE_KEY_PODCAST_PREFIX}{podcast_id}",
            base_key=self.provider.lookup_key,
            category=CACHE_CATEGORY_PODCASTS,
        )
        await self.mass.cache.delete(
            key=f"{CACHE_KEY_EPISODES_PREFIX}{podcast_id}",
            base_key=self.provider.lookup_key,
            category=CACHE_CATEGORY_EPISODES,
        )

    # Core podcast methods
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id with caching."""
        # Try cache first
        cached_data = await self._cache_get_podcast(prov_podcast_id)
        if cached_data:
            self.logger.debug(f"Using cached data for podcast {prov_podcast_id}")
            return parse_podcast(cached_data, self.provider)

        # Fetch from API if not cached
        podcast_obj = await self.provider._get_data(f"shows/{prov_podcast_id}")

        # Add a check to ensure podcast_obj is not None
        if not podcast_obj:
            raise ValueError(f"No podcast data returned from API for ID: {prov_podcast_id}")

        await self._cache_set_podcast(prov_podcast_id, podcast_obj)
        return parse_podcast(podcast_obj, self.provider)

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all podcast episodes with caching and played status sync to MA."""
        # Try cache first
        cached_episodes = await self._cache_get_episodes(prov_podcast_id)
        if cached_episodes:
            self.logger.debug(f"Using cached episodes for podcast {prov_podcast_id}")
            podcast = await self.get_podcast(prov_podcast_id)

            for episode_data in cached_episodes:
                episode = parse_podcast_episode(episode_data, self.provider, podcast=podcast)
                # Handle position assignment for cached episodes
                if hasattr(episode, "_position"):
                    episode.position = episode._position
                yield episode
            return

        # Fetch fresh data from API
        podcast = await self.get_podcast(prov_podcast_id)
        episodes_to_cache: list[dict[str, Any]] = []
        synced_count = 0
        total_count = 0
        episode_position = 1

        # Paginate through all episodes from Spotify
        page_size = 50
        offset = 0

        while True:
            episodes_data = await self.provider._get_data(
                f"shows/{prov_podcast_id}/episodes",
                limit=page_size,
                offset=offset,
                market="from_token",  # Critical for getting user's resume_point data
            )

            if episodes_data is None:
                break

            if not episodes_data.get("items"):
                break

            for item in episodes_data["items"]:
                if not (item and item["id"]):
                    continue

                episode = parse_podcast_episode(item, self.provider, podcast=podcast)
                episode.position = episode_position
                episode_position += 1

                # Store position for caching
                item["_position"] = episode.position
                episodes_to_cache.append(item)

                total_count += 1

                # Sync played status to MA playlog after episode is fully parsed
                if self.sync_played_status_enabled and hasattr(episode, "fully_played"):
                    try:
                        if episode.fully_played:
                            # Mark as played in MA's playlog
                            await self.mass.music.mark_item_played(
                                episode,
                                fully_played=True,
                                seconds_played=(
                                    episode.resume_position_ms // 1000
                                    if episode.resume_position_ms
                                    else episode.duration
                                ),
                            )
                            synced_count += 1
                        elif (
                            episode.resume_position_ms is not None
                            and episode.resume_position_ms > 0
                        ):
                            # Mark with resume position but not fully played
                            await self.mass.music.mark_item_played(
                                episode,
                                fully_played=False,
                                seconds_played=episode.resume_position_ms // 1000,
                            )
                    except Exception as e:
                        self.logger.warning(
                            f"Failed to sync played status to MA for episode {episode.item_id}: {e}"
                        )

                yield episode

            # Check if there are more episodes
            if len(episodes_data["items"]) < page_size:
                break

            offset += page_size

        # Cache the episodes data
        if episodes_to_cache:
            await self._cache_set_episodes(prov_podcast_id, episodes_to_cache)

        if self.sync_played_status_enabled and total_count > 0:
            self.logger.info(
                f"Retrieved {total_count} episodes for podcast {prov_podcast_id}, "
                f"{synced_count} marked as played from Spotify"
            )

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id with played status sync."""
        episode_obj = await self.provider._get_data(
            f"episodes/{prov_episode_id}", market="from_token"
        )

        if episode_obj is None:
            raise ValueError(f"No episode data returned from API for ID: {prov_episode_id}")

        episode = parse_podcast_episode(episode_obj, self.provider)

        # Sync individual episode to MA if needed
        if (
            self.sync_played_status_enabled
            and hasattr(episode, "fully_played")
            and episode.fully_played
        ):
            try:
                await self.mass.music.mark_item_played(
                    episode,
                    fully_played=True,
                    seconds_played=episode.resume_position_ms // 1000
                    if episode.resume_position_ms
                    else episode.duration,
                )
                self.logger.debug(
                    f"Synced individual episode played status from Spotify: {prov_episode_id}"
                )
            except Exception as e:
                self.logger.warning(f"Failed to sync individual episode to MA: {e}")

        return episode

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """
        Get resume position for episode from Spotify.

        Returns:
            tuple[bool, int]: (is_fully_played, position_in_milliseconds)
        """
        if media_type != MediaType.PODCAST_EPISODE:
            raise NotImplementedError("Resume position only supported for podcast episodes")

        try:
            # Get latest episode data from Spotify
            episode_obj = await self.provider._get_data(f"episodes/{item_id}", market="from_token")

            # Add a check to handle the case where episode_obj is None
            if episode_obj is None:
                raise NotImplementedError("No resume point data from Spotify")

            if "resume_point" not in episode_obj or not episode_obj["resume_point"]:
                # No resume point data available, let MA use its stored position
                raise NotImplementedError("No resume point data from Spotify")

            resume_point = episode_obj["resume_point"]
            fully_played = resume_point.get("fully_played", False)
            position_ms = resume_point.get("resume_position_ms", 0)

            # Apply played threshold logic
            if not fully_played and episode_obj.get("duration_ms", 0) > 0:
                completion_ratio = position_ms / episode_obj["duration_ms"]
                if completion_ratio >= self.played_threshold:
                    fully_played = True
                    self.logger.debug(
                        f"Episode {item_id} marked as played due to {completion_ratio:.1%} "
                        f"completion"
                    )

            self.logger.debug(
                f"Resume position from Spotify for {item_id}: {position_ms}ms, "
                f"played: {fully_played}"
            )
            return fully_played, position_ms

        except Exception as e:
            self.logger.debug(f"Failed to get resume position from Spotify for {item_id}: {e}")
            # Let MA fall back to its stored resume position
            raise NotImplementedError("Failed to get resume position from Spotify")

    async def bulk_sync_podcast_to_ma(self, podcast_id: str) -> dict[str, int]:
        """
        Bulk sync all episodes in a podcast from Spotify to MA with caching.

        This is useful for initial sync or when re-syncing a podcast.
        """
        stats = {"synced_played": 0, "synced_positions": 0, "errors": 0, "total": 0}

        if not self.sync_played_status_enabled:
            self.logger.info(f"Played status sync disabled, skipping bulk sync for {podcast_id}")
            return stats

        try:
            self.logger.info(f"Starting bulk sync from Spotify to MA for podcast {podcast_id}")

            # Invalidate cache to ensure fresh data during sync
            await self._cache_invalidate_podcast(podcast_id)

            async for episode in self.get_podcast_episodes(podcast_id):
                stats["total"] += 1

                try:
                    if hasattr(episode, "fully_played") and episode.fully_played:
                        stats["synced_played"] += 1
                    elif (
                        hasattr(episode, "resume_position_ms")
                        and episode.resume_position_ms is not None
                        and episode.resume_position_ms > 0
                    ):
                        stats["synced_positions"] += 1

                except Exception as e:
                    self.logger.warning(f"Error syncing episode {episode.item_id}: {e}")
                    stats["errors"] += 1

        except Exception as e:
            self.logger.error(f"Error during bulk sync for podcast {podcast_id}: {e}")
            stats["errors"] += 1

        self.logger.info(f"Bulk sync completed for {podcast_id}: {stats}")
        return stats

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Call when an episode is played in MA.

        Note: This CANNOT sync back to Spotify as there's no API for it.
        This is just for logging/monitoring purposes.
        """
        if media_type != MediaType.PODCAST_EPISODE:
            return

        if not isinstance(media_item, PodcastEpisode):
            return

        # Handle case where position might be None (e.g., when marked as played in UI)
        safe_position = position or 0
        if media_item.duration > 0:
            completion_percentage = (safe_position / media_item.duration) * 100
        else:
            completion_percentage = 0
        self.logger.debug(
            f"Episode played in MA: {prov_item_id} "
            f"({completion_percentage:.1f}%, fully_played: {fully_played}) "
            f"- Cannot sync back to Spotify due to API limitations"
        )

    # Cache warming methods
    async def warm_podcast_cache(self, podcast_id: str) -> None:
        """Warm the cache for a podcast and its episodes."""
        try:
            # Get podcast data
            podcast_obj = await self.provider._get_data(f"shows/{podcast_id}")

            if podcast_obj is None:
                self.logger.debug(
                    f"No podcast data returned for ID: {podcast_id}, skipping cache warm."
                )
                return

            await self._cache_set_podcast(podcast_id, podcast_obj)

            # Get first page of episodes to warm cache
            episodes_data = await self.provider._get_data(
                f"shows/{podcast_id}/episodes",
                limit=50,
                market="from_token",
            )

            # Add a check to ensure episodes_data is not None before using it
            if episodes_data and episodes_data.get("items"):
                await self._cache_set_episodes(podcast_id, episodes_data["items"])

            self.logger.debug(f"Warmed cache for podcast {podcast_id}")
        except Exception as e:
            self.logger.warning(f"Failed to warm cache for podcast {podcast_id}: {e}")

    async def warm_library_podcast_cache(self) -> None:
        """Warm cache for all library podcasts."""
        try:
            # Get all library podcasts
            podcasts = []
            async for podcast in self.provider.get_library_podcasts():
                podcasts.append(podcast.item_id)

            # Warm cache in batches to avoid overwhelming the API
            batch_size = 5
            for i in range(0, len(podcasts), batch_size):
                batch = podcasts[i : i + batch_size]
                tasks = [self.warm_podcast_cache(podcast_id) for podcast_id in batch]
                await asyncio.gather(*tasks, return_exceptions=True)

                # Small delay between batches to be API-friendly
                if i + batch_size < len(podcasts):
                    await asyncio.sleep(1)

            self.logger.info(f"Warmed cache for {len(podcasts)} library podcasts")
        except Exception as e:
            self.logger.warning(f"Failed to warm library podcast cache: {e}")

    # Cache management methods
    async def clear_podcast_cache(self) -> None:
        """Clear all podcast-related cached data."""
        try:
            # Clear podcast categories
            for category in [CACHE_CATEGORY_PODCASTS, CACHE_CATEGORY_EPISODES]:
                await self.mass.cache.clear(
                    category=category, base_key_filter=self.provider.lookup_key
                )
            self.logger.info("Successfully cleared podcast cached data")
        except Exception as e:
            self.logger.warning(f"Failed to clear podcast cache: {e}")


# Utility function for syncing all podcasts from Spotify to MA with enhanced caching
async def sync_all_podcasts_from_spotify(provider: SpotifyProvider) -> dict[str, Any]:
    """
    Sync all subscribed podcasts from Spotify to MA with cache optimization.

    This could be called during initial setup or manual re-sync.
    """
    overall_stats = {"podcasts_synced": 0, "total_episodes": 0, "total_played": 0, "errors": 0}

    if not provider.podcast_manager.sync_played_status_enabled:
        provider.logger.info("Played status sync disabled")
        return overall_stats

    provider.logger.info("Starting full podcast library sync from Spotify to MA")

    try:
        # Clear cache before full sync to ensure fresh data
        await provider.podcast_manager.clear_podcast_cache()

        async for podcast in provider.get_library_podcasts():
            try:
                stats = await provider.podcast_manager.bulk_sync_podcast_to_ma(podcast.item_id)
                overall_stats["podcasts_synced"] += 1
                overall_stats["total_episodes"] += stats["total"]
                overall_stats["total_played"] += stats["synced_played"]
                overall_stats["errors"] += stats["errors"]

            except Exception as e:
                provider.logger.error(f"Failed to sync podcast {podcast.item_id}: {e}")
                overall_stats["errors"] += 1

    except Exception as e:
        provider.logger.error(f"Error during full library sync: {e}")
        overall_stats["errors"] += 1

    provider.logger.info(f"Full library sync completed: {overall_stats}")
    return overall_stats
