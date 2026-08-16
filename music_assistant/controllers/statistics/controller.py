"""Statistics Controller implementation."""

from __future__ import annotations

import functools
import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import ItemMapping
from music_assistant_models.media_items.metadata import MediaItemImage
from music_assistant_models.statistics import DistributionItem, TopItemResult

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PLAYLOG,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.json import SerializableType, json_loads
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant import MusicAssistant

# Statistics cache TTL: 5 minutes
STATISTICS_CACHE_TTL = 300

T = TypeVar("T")


def cache_statistics(
    ttl: int = STATISTICS_CACHE_TTL,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Cache decorator for statistics methods with TTL."""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        cache: dict[str, tuple[float, Any]] = {}

        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            user = get_current_user()
            user_id = user.user_id if user else "anonymous"

            cache_key_parts = [func.__name__, user_id, *args]
            for key in sorted(kwargs.keys()):
                cache_key_parts.append(f"{key}={kwargs[key]}")
            cache_key = hashlib.md5("|".join(str(p) for p in cache_key_parts).encode()).hexdigest()

            now = time.time()
            if cache_key in cache:
                cached_time, cached_value = cache[cache_key]
                if now - cached_time < ttl:
                    return cast("T", cached_value)

            result = await func(self, *args, **kwargs)
            cache[cache_key] = (now, result)
            return result

        return wrapper

    return decorator


class StatisticsController(CoreController):
    """Controller handling playlog statistics and analytics."""

    domain: str = "statistics"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        self.manifest.name = "Statistics controller"
        self.manifest.description = (
            "Music Assistant's core controller for playlog statistics and listening analytics."
        )
        self.manifest.icon = "poll"

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of statistics module."""
        self.logger.info("Initializing statistics controller...")

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        return {
            "playlog_entries": await self.mass.music.database.get_count(DB_TABLE_PLAYLOG),
        }

    @cache_statistics()
    @api_command("statistics/top_items", required_scope=Scope.LIBRARY_READ)
    async def get_top_items(
        self,
        media_type: MediaType,
        period: str = "week",
        limit: int = 50,
    ) -> list[TopItemResult]:
        """
        Get most played items in a time period.

        Returns list of TopItemResult with 'item' (ItemMapping) and 'play_count' (int).
        Supports ALL MediaTypes: Track, Album, Artist, Radio, Audiobook, Genre, Podcast, etc.

        :param media_type: The type of media to get top items for.
        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param limit: Maximum number of items to return (default 50).
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []

        cutoff_timestamp = self._get_period_cutoff(period)

        query = f"""
            SELECT
                item_id,
                provider,
                media_type,
                MAX(name) as name,
                MAX(image) as image,
                COUNT(*) as play_count,
                SUM(seconds_played) as total_seconds,
                MIN(timestamp) as first_played,
                MAX(timestamp) as last_played
            FROM {DB_TABLE_PLAYLOG}
            WHERE userid = :user_id
                AND media_type = :media_type
                AND timestamp >= :cutoff_timestamp
            GROUP BY item_id, provider, media_type
            ORDER BY play_count DESC
        """

        params = {
            "user_id": user_id,
            "media_type": media_type.value,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        available_providers = ("library", *get_global_cache_value("available_providers", []))

        result: list[TopItemResult] = []

        for row in rows:
            provider = row["provider"]

            # Parse image from DB and convert to MediaItemImage object
            image = None
            if row["image"]:
                image_dict = json_loads(row["image"])
                if image_dict:
                    # Fix provider instance IDs to domain-only
                    if "provider" in image_dict and "--" in image_dict["provider"]:
                        image_dict["provider"] = image_dict["provider"].split("--")[0]
                    image = MediaItemImage.from_dict(image_dict)

            item_mapping = ItemMapping(
                item_id=row["item_id"],
                provider=provider,
                media_type=MediaType(row["media_type"]),
                name=row["name"],
                image=image,
                available=provider in available_providers,
            )

            result.append(
                TopItemResult(
                    item=item_mapping,
                    play_count=row["play_count"],
                )
            )

        return result

    @cache_statistics()
    @api_command("statistics/genre_distribution", required_scope=Scope.LIBRARY_READ)
    async def get_genre_distribution(
        self,
        period: str = "week",
        limit: int = 10,
    ) -> list[dict[str, str | int]]:
        """
        Get genre distribution (play counts by genre).

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param limit: Maximum number of genres to return (default 10).
        """
        # TODO: Implement genre extraction from playlog
        # For now, return empty list - requires genre metadata in playlog
        return []

    @cache_statistics()
    @api_command("statistics/artist_distribution", required_scope=Scope.LIBRARY_READ)
    async def get_artist_distribution(
        self,
        period: str = "week",
        limit: int = 10,
    ) -> list[TopItemResult]:
        """
        Get artist distribution based on track plays (play counts by artist).

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param limit: Maximum number of artists to return (default 10).
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []

        cutoff_timestamp = self._get_period_cutoff(period)

        # Extract artists from track plays and join with artists table for images
        # Each track can have multiple artists, so we use json_each to expand the array
        query = f"""
            WITH artist_plays AS (
                SELECT
                    json_extract(artist_data.value, '$.item_id') as item_id,
                    json_extract(artist_data.value, '$.provider') as provider,
                    json_extract(artist_data.value, '$.name') as name
                FROM {DB_TABLE_PLAYLOG},
                     json_each(artists) as artist_data
                WHERE userid = :user_id
                    AND media_type = :media_type
                    AND timestamp >= :cutoff_timestamp
                    AND artists IS NOT NULL
            )
            SELECT
                ap.item_id,
                ap.provider,
                ap.name,
                a.item_id as library_item_id,
                a.metadata as metadata,
                COUNT(*) as play_count
            FROM artist_plays ap
            LEFT JOIN {DB_TABLE_ARTISTS} a ON LOWER(a.name) = LOWER(ap.name)
            GROUP BY ap.item_id, ap.provider
            ORDER BY play_count DESC
        """

        params = {
            "user_id": user_id,
            "media_type": MediaType.TRACK.value,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        available_providers = ("library", *get_global_cache_value("available_providers", []))

        result: list[TopItemResult] = []

        for row in rows:
            provider = row["provider"]

            # If artist exists in library, use library ID and provider
            if row["library_item_id"]:
                item_id = str(row["library_item_id"])
                provider = "library"
            else:
                item_id = row["item_id"]

            # Extract image from artist metadata
            image = None
            if row["metadata"]:
                metadata_dict = json_loads(row["metadata"])
                if metadata_dict and "images" in metadata_dict and metadata_dict["images"]:
                    # Get first thumb image from metadata
                    for img in metadata_dict["images"]:
                        if img.get("type") == ImageType.THUMB.value:
                            image = MediaItemImage.from_dict(img)
                            break

            item_mapping = ItemMapping(
                item_id=item_id,
                provider=provider,
                media_type=MediaType.ARTIST,
                name=row["name"],
                image=image,
                available=provider in available_providers,
            )

            result.append(
                TopItemResult(
                    item=item_mapping,
                    play_count=row["play_count"],
                )
            )

        return result

    @cache_statistics()
    @api_command("statistics/plays_over_time", required_scope=Scope.LIBRARY_READ)
    async def get_plays_over_time(
        self,
        period: str = "week",
        granularity: str = "day",
    ) -> list[dict[str, str | int]]:
        """
        Get play counts over time (time series data).

        :param period: Time period - 'today', 'week', 'month', 'year'.
        :param granularity: Time bucket size - 'hour', 'day', 'week', 'month'.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []

        cutoff_timestamp = self._get_period_cutoff(period)

        # SQLite date formatting based on granularity
        date_format = {
            "hour": "%Y-%m-%d %H:00:00",
            "day": "%Y-%m-%d",
            "week": "%Y-W%W",
            "month": "%Y-%m",
        }.get(granularity, "%Y-%m-%d")

        query = f"""
            SELECT
                strftime('{date_format}', datetime(timestamp, 'unixepoch')) as time_bucket,
                COUNT(*) as play_count
            FROM {DB_TABLE_PLAYLOG}
            WHERE userid = :user_id
                AND timestamp >= :cutoff_timestamp
            GROUP BY time_bucket
            ORDER BY time_bucket ASC
        """

        params = {
            "user_id": user_id,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params)

        return [{"timestamp": row["time_bucket"], "value": row["play_count"]} for row in rows]

    @cache_statistics()
    @api_command("statistics/listening_activity", required_scope=Scope.LIBRARY_READ)
    async def get_listening_activity(
        self,
        period: str = "week",
    ) -> list[dict[str, int]]:
        """
        Get listening activity heatmap (plays by hour of day and weekday).

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []

        cutoff_timestamp = self._get_period_cutoff(period)

        query = f"""
            SELECT
                CAST(strftime('%H', datetime(timestamp, 'unixepoch')) AS INTEGER) as hour,
                CAST((CAST(strftime('%w', datetime(timestamp, 'unixepoch')) AS INTEGER) + 6) % 7 AS INTEGER) as weekday,
                COUNT(*) as play_count
            FROM {DB_TABLE_PLAYLOG}
            WHERE userid = :user_id
                AND timestamp >= :cutoff_timestamp
            GROUP BY hour, weekday
            ORDER BY weekday, hour
        """

        params = {
            "user_id": user_id,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params)

        return [
            {"hour": row["hour"], "weekday": row["weekday"], "value": row["play_count"]}
            for row in rows
        ]

    @cache_statistics()
    @api_command("statistics/listening_time", required_scope=Scope.LIBRARY_READ)
    async def get_listening_time(
        self,
        period: str = "week",
        group_by: str = "artist",
        limit: int = 10,
    ) -> list[dict[str, str | float]]:
        """
        Get total listening time grouped by artist or genre.

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param group_by: Group by 'artist' or 'genre' (default 'artist').
        :param limit: Maximum number of items to return (default 10).
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []

        cutoff_timestamp = self._get_period_cutoff(period)

        # Group by artist using artist plays
        # Estimate listening time: each artist play ≈ 3 minutes (180 seconds)
        if group_by != "artist":
            # Only artist grouping supported for now
            return []

        query = f"""
            SELECT
                item_id,
                provider,
                name,
                COUNT(*) * 180 as estimated_seconds
            FROM {DB_TABLE_PLAYLOG}
            WHERE userid = :user_id
                AND media_type = :media_type
                AND timestamp >= :cutoff_timestamp
            GROUP BY item_id, provider
            ORDER BY estimated_seconds DESC
        """

        params = {
            "user_id": user_id,
            "media_type": MediaType.ARTIST.value,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        return [
            {"name": row["name"], "minutes": round(row["estimated_seconds"] / 60, 1)}
            for row in rows
            if row["estimated_seconds"] is not None and row["estimated_seconds"] > 0
        ]

    @cache_statistics()
    @api_command("statistics/decade_distribution", required_scope=Scope.LIBRARY_READ)
    async def get_decade_distribution(
        self,
        period: str = "all_time",
        limit: int = 10,
    ) -> list[DistributionItem]:
        """
        Get play counts grouped by decade.

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param limit: Maximum number of decades to return.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []

        cutoff_timestamp = self._get_period_cutoff(period)

        query = f"""
            SELECT
                (albums.year / 10) * 10 as decade,
                COUNT(*) as play_count
            FROM {DB_TABLE_PLAYLOG} as playlog
            INNER JOIN {DB_TABLE_ALBUM_TRACKS} as album_tracks
                ON playlog.item_id = album_tracks.track_id
            INNER JOIN {DB_TABLE_ALBUMS} as albums
                ON album_tracks.album_id = albums.item_id
            WHERE playlog.userid = :user_id
                AND playlog.media_type = :media_type
                AND playlog.timestamp >= :cutoff_timestamp
                AND albums.year IS NOT NULL
            GROUP BY decade
            ORDER BY decade DESC
        """

        params = {
            "user_id": user_id,
            "media_type": MediaType.TRACK.value,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        return [{"name": f"{int(row['decade'])}s", "value": row["play_count"]} for row in rows]

    @cache_statistics()
    @api_command("statistics/play_history", required_scope=Scope.LIBRARY_READ)
    async def get_play_history(
        self,
        limit: int = 50,
        media_types: list[MediaType] | None = None,
        played_after_timestamp: int | None = None,
    ) -> list[ItemMapping]:
        """
        Get paginated play history with optional time range filtering.

        Shows complete user history without provider availability filtering.

        :param limit: Maximum number of items to return (default 50).
        :param media_types: Optional list of media types to filter by.
        :param played_after_timestamp: Optional timestamp to filter items played after.
        """
        user = get_current_user()
        if not user:
            return []
        user_id = user.user_id

        if media_types is None:
            media_types = MediaType.ALL
        media_types_str = "(" + ",".join(f'"{x.value}"' for x in media_types) + ")"

        query = f"""
            SELECT *
            FROM {DB_TABLE_PLAYLOG}
            WHERE userid = :user_id
                AND media_type IN {media_types_str}
        """

        params: dict[str, Any] = {"user_id": user_id}

        if played_after_timestamp is not None:
            query += " AND timestamp >= :played_after_timestamp"
            params["played_after_timestamp"] = played_after_timestamp

        query += " ORDER BY timestamp DESC"

        db_rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        available_providers = ("library", *get_global_cache_value("available_providers", []))

        return [
            ItemMapping.from_dict(
                {
                    "item_id": db_row["item_id"],
                    "provider": db_row["provider"],
                    "media_type": db_row["media_type"],
                    "name": db_row["name"],
                    "image": json_loads(db_row["image"]) if db_row["image"] else None,
                    "available": db_row["provider"] in available_providers,
                }
            )
            for db_row in db_rows
        ]

    @cache_statistics()
    @api_command("statistics/play_count", required_scope=Scope.LIBRARY_READ)
    async def get_play_count(
        self,
        item_id: str,
        provider: str,
        media_type: MediaType,
    ) -> int:
        """
        Get play count for a specific item.

        :param item_id: The item ID to get play count for.
        :param provider: The provider ID.
        :param media_type: The media type.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return 0

        query = f"""
            SELECT COUNT(*) as count
            FROM {DB_TABLE_PLAYLOG}
            WHERE userid = :user_id
                AND item_id = :item_id
                AND provider = :provider
                AND media_type = :media_type
        """

        params = {
            "user_id": user_id,
            "item_id": item_id,
            "provider": provider,
            "media_type": media_type.value,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=1)
        return int(rows[0]["count"]) if rows else 0

    @cache_statistics()
    @api_command("statistics/played_item_ids", required_scope=Scope.LIBRARY_READ)
    async def get_played_item_ids(
        self,
        media_type: MediaType,
        since_timestamp: float,
    ) -> set[tuple[str, str]]:
        """
        Get set of (provider, item_id) tuples played since timestamp.

        Used for temporal filters in smart playlists and recommendations.

        :param media_type: The media type to filter by.
        :param since_timestamp: Unix timestamp to filter items played after.
        """
        user = get_current_user()
        if not user:
            return set()
        user_id = user.user_id

        query = f"""
            SELECT DISTINCT provider, item_id
            FROM {DB_TABLE_PLAYLOG}
            WHERE userid = :user_id
                AND media_type = :media_type
                AND timestamp >= :since_timestamp
        """

        params = {
            "user_id": user_id,
            "media_type": media_type.value,
            "since_timestamp": since_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params)
        return {(row["provider"], row["item_id"]) for row in rows}

    def _get_period_cutoff(self, period: str) -> float:
        """
        Get the cutoff timestamp for a time period.

        :param period: Time period string ('today', 'week', 'month', 'year', 'all_time').
        :return: Unix timestamp for the start of the period.
        """
        # TODO: Use user timezone instead of UTC for accurate "today"/calendar boundaries
        now = time.time()

        if period == "today":
            # Start of today (midnight UTC) - calculate seconds since midnight
            seconds_since_midnight = now % 86400
            return now - seconds_since_midnight
        if period == "week":
            # 7 days ago
            return now - (7 * 86400)
        if period == "month":
            # 30 days ago
            return now - (30 * 86400)
        if period == "year":
            # 365 days ago
            return now - (365 * 86400)
        if period == "all_time":
            # Beginning of time (0)
            return 0.0
        # Default to week
        return now - (7 * 86400)
