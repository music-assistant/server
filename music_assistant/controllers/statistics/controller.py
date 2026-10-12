"""Statistics Controller implementation."""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from music_assistant_models.auth import Scope
from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import ItemMapping
from music_assistant_models.media_items.metadata import MediaItemImage
from music_assistant_models.statistics import (
    DistributionItem,
    HeatmapPoint,
    ListeningTimeItem,
    TimeSeriesPoint,
    TopItemResult,
)

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_PLAY_HISTORY,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.json import SerializableType, json_loads
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant import MusicAssistant

# Statistics cache TTL: 5 minutes
STATISTICS_CACHE_TTL = 300
MAX_STATISTICS_RESULT_LIMIT = 500
_PLAY_HISTORY_EVENT_TYPES = (
    MediaType.TRACK.value,
    MediaType.RADIO.value,
    MediaType.AUDIOBOOK.value,
    MediaType.PODCAST_EPISODE.value,
)
_PLAY_HISTORY_EVENT_TYPE_SQL = ", ".join(
    f"'{media_type}'" for media_type in _PLAY_HISTORY_EVENT_TYPES
)
_TRACK_HISTORY_CTE = f"""
    history_tracks AS (
        SELECT play_history.id AS play_id, provider_mappings.item_id AS track_id
        FROM {DB_TABLE_PLAY_HISTORY} AS play_history
        INNER JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS provider_mappings
            ON provider_mappings.media_type = play_history.media_type
            AND provider_mappings.provider_instance = play_history.provider
            AND provider_mappings.provider_item_id = play_history.item_id
        WHERE play_history.userid = :user_id
            AND play_history.media_type = :media_type
            AND play_history.timestamp >= :cutoff_timestamp
        UNION
        SELECT play_history.id AS play_id, CAST(play_history.item_id AS INTEGER) AS track_id
        FROM {DB_TABLE_PLAY_HISTORY} AS play_history
        WHERE play_history.userid = :user_id
            AND play_history.media_type = :media_type
            AND play_history.provider = 'library'
            AND play_history.timestamp >= :cutoff_timestamp
    )
"""

T = TypeVar("T")


def _validate_statistics_limit(limit: int) -> int:
    if limit < 1:
        raise InvalidDataError("Statistics limit must be at least 1")
    return min(limit, MAX_STATISTICS_RESULT_LIMIT)


def cache_statistics(
    ttl: int = STATISTICS_CACHE_TTL,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Cache statistics through the shared cache controller, scoped by user and generation."""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @use_cache(expiration=ttl)
        @functools.wraps(func)
        async def cached_method(
            self: Any,
            _user_id: str,
            _cache_generation: str,
            *args: Any,
            **kwargs: Any,
        ) -> T:
            return await func(self, *args, **kwargs)

        @functools.wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> T:
            user = get_current_user()
            user_id = user.user_id if user else "anonymous"
            return await cached_method(
                self,
                user_id,
                self._statistics_cache_generation,
                *args,
                **kwargs,
            )

        return wrapper

    return decorator


class StatisticsController(CoreController):
    """Controller handling playlog statistics and analytics."""

    domain: str = "statistics"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        self._statistics_cache_generation = uuid4().hex
        self.manifest.name = "Statistics controller"
        self.manifest.description = (
            "Music Assistant's core controller for playlog statistics and listening analytics."
        )
        self.manifest.icon = "poll"

    def invalidate_statistics_cache(self, *, play_history_written: bool) -> None:
        """Invalidate prior statistics cache keys after a completed play is recorded."""
        if play_history_written:
            self._statistics_cache_generation = uuid4().hex

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of statistics module."""
        self.logger.info("Initializing statistics controller...")

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        return {
            "playlog_entries": await self.mass.music.database.get_count(DB_TABLE_PLAY_HISTORY),
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
        limit = _validate_statistics_limit(limit)

        cutoff_timestamp = self._get_period_cutoff(period)

        query = f"""
            SELECT
                item_id,
                provider,
                media_type,
                MAX(name) as name,
                MAX(image) as image,
                COUNT(*) as play_count
            FROM {DB_TABLE_PLAY_HISTORY}
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
    ) -> list[DistributionItem]:
        """
        Get genre distribution (play counts by genre).

        Counts plays for tracks with known library genre mappings.

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param limit: Maximum number of genres to return (default 10).
        """
        user = get_current_user()
        if not user:
            return []

        limit = _validate_statistics_limit(limit)
        query = f"""
            WITH {_TRACK_HISTORY_CTE}
            SELECT genres.name, COUNT(*) as play_count
            FROM history_tracks
            INNER JOIN {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} as genre_mappings
                ON genre_mappings.media_id = history_tracks.track_id
                AND genre_mappings.media_type = :media_type
            INNER JOIN {DB_TABLE_GENRES} as genres
                ON genres.item_id = genre_mappings.genre_id
            GROUP BY genres.item_id
            ORDER BY play_count DESC, genres.sort_name
        """

        params = {
            "user_id": user.user_id,
            "media_type": MediaType.TRACK.value,
            "cutoff_timestamp": self._get_period_cutoff(period),
        }
        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        return [DistributionItem(name=row["name"], value=row["play_count"]) for row in rows]

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
        limit = _validate_statistics_limit(limit)

        cutoff_timestamp = self._get_period_cutoff(period)

        # Extract artists from track plays and join with artists table for images
        # Each track can have multiple artists, so we use json_each to expand the array
        query = f"""
            WITH artist_plays AS (
                SELECT
                    json_extract(artist_data.value, '$.item_id') as item_id,
                    json_extract(artist_data.value, '$.provider') as provider,
                    json_extract(artist_data.value, '$.name') as name
                FROM {DB_TABLE_PLAY_HISTORY},
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
    ) -> list[TimeSeriesPoint]:
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
        date_formats = {
            "hour": "%Y-%m-%d %H:00:00",
            "day": "%Y-%m-%d",
            "week": "%Y-W%W",
            "month": "%Y-%m",
        }
        if granularity not in date_formats:
            raise InvalidDataError("granularity must be hour, day, week, or month")
        date_format = date_formats[granularity]

        query = f"""
            SELECT
                strftime('{date_format}', datetime(timestamp, 'unixepoch')) as time_bucket,
                COUNT(*) as play_count
            FROM {DB_TABLE_PLAY_HISTORY}
            WHERE userid = :user_id
                AND timestamp >= :cutoff_timestamp
                AND media_type IN ({_PLAY_HISTORY_EVENT_TYPE_SQL})
            GROUP BY time_bucket
            ORDER BY time_bucket ASC
        """

        params = {
            "user_id": user_id,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=0)

        return [
            TimeSeriesPoint(timestamp=row["time_bucket"], value=row["play_count"]) for row in rows
        ]

    @cache_statistics()
    @api_command("statistics/listening_activity", required_scope=Scope.LIBRARY_READ)
    async def get_listening_activity(
        self,
        period: str = "week",
    ) -> list[HeatmapPoint]:
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
            FROM {DB_TABLE_PLAY_HISTORY}
            WHERE userid = :user_id
                AND timestamp >= :cutoff_timestamp
                AND media_type IN ({_PLAY_HISTORY_EVENT_TYPE_SQL})
            GROUP BY hour, weekday
            ORDER BY weekday, hour
        """

        params = {
            "user_id": user_id,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params)

        return [
            HeatmapPoint(hour=row["hour"], weekday=row["weekday"], value=row["play_count"])
            for row in rows
        ]

    @cache_statistics()
    @api_command("statistics/listening_time", required_scope=Scope.LIBRARY_READ)
    async def get_listening_time(
        self,
        period: str = "week",
        group_by: str = "artist",
        limit: int = 10,
    ) -> list[ListeningTimeItem]:
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
        limit = _validate_statistics_limit(limit)
        if group_by not in ("artist", "genre"):
            raise InvalidDataError("group_by must be 'artist' or 'genre'")

        cutoff_timestamp = self._get_period_cutoff(period)

        if group_by == "artist":
            query = f"""
                SELECT
                    json_extract(artist_data.value, '$.item_id') as artist_item_id,
                    json_extract(artist_data.value, '$.provider') as artist_provider,
                    MAX(json_extract(artist_data.value, '$.name')) as name,
                    COUNT(*) * 180 as estimated_seconds
                FROM {DB_TABLE_PLAY_HISTORY} as play_history,
                    json_each(play_history.artists) as artist_data
                WHERE play_history.userid = :user_id
                    AND play_history.media_type = :media_type
                    AND play_history.timestamp >= :cutoff_timestamp
                    AND play_history.artists IS NOT NULL
                GROUP BY artist_item_id, artist_provider
                ORDER BY estimated_seconds DESC
            """
        else:
            query = f"""
                WITH {_TRACK_HISTORY_CTE}, genre_plays AS (
                    SELECT DISTINCT
                        history_tracks.play_id,
                        genres.item_id AS genre_id,
                        genres.name AS name
                    FROM history_tracks
                    INNER JOIN {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} AS genre_mappings
                        ON genre_mappings.media_id = history_tracks.track_id
                        AND genre_mappings.media_type = :media_type
                    INNER JOIN {DB_TABLE_GENRES} AS genres
                        ON genres.item_id = genre_mappings.genre_id
                )
                SELECT name, COUNT(*) * 180 as estimated_seconds
                FROM genre_plays
                GROUP BY genre_id
                ORDER BY estimated_seconds DESC, name
            """

        params = {
            "user_id": user_id,
            "media_type": MediaType.TRACK.value,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        return [
            ListeningTimeItem(name=row["name"], minutes=round(row["estimated_seconds"] / 60, 1))
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

        Tracks linked to multiple albums use their earliest known album year.

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param limit: Maximum number of decades to return.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []
        limit = _validate_statistics_limit(limit)

        cutoff_timestamp = self._get_period_cutoff(period)

        query = f"""
            WITH {_TRACK_HISTORY_CTE}, track_decades AS (
                SELECT album_tracks.track_id, MIN(albums.year) AS year
                FROM {DB_TABLE_ALBUM_TRACKS} as album_tracks
                INNER JOIN {DB_TABLE_ALBUMS} as albums
                    ON album_tracks.album_id = albums.item_id
                WHERE albums.year IS NOT NULL
                GROUP BY album_tracks.track_id
            ), history_decades AS (
                SELECT history_tracks.play_id, (track_decades.year / 10) * 10 AS decade
                FROM history_tracks
                INNER JOIN track_decades
                    ON history_tracks.track_id = track_decades.track_id
            )
            SELECT decade, COUNT(*) as play_count
            FROM history_decades
            GROUP BY decade
            ORDER BY decade DESC
        """

        params = {
            "user_id": user_id,
            "media_type": MediaType.TRACK.value,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        return [
            DistributionItem(name=f"{int(row['decade'])}s", value=row["play_count"]) for row in rows
        ]

    @cache_statistics()
    @api_command("statistics/play_history", required_scope=Scope.LIBRARY_READ)
    async def get_play_history(
        self,
        limit: int = 50,
        media_types: list[MediaType] | None = None,
        played_after_timestamp: int | None = None,
        offset: int = 0,
    ) -> list[ItemMapping]:
        """
        Get paginated play history with optional time range filtering.

        Shows complete user history without provider availability filtering.

        :param limit: Maximum number of items to return (default 50).
        :param media_types: Optional list of media types to filter by.
        :param played_after_timestamp: Optional timestamp to filter items played after.
        :param offset: Number of matching items to skip.
        """
        user = get_current_user()
        if not user:
            return []
        user_id = user.user_id

        if limit < 1:
            raise InvalidDataError("Play history limit must be at least 1")
        if offset < 0:
            raise InvalidDataError("Play history offset cannot be negative")
        limit = min(limit, MAX_STATISTICS_RESULT_LIMIT)

        if media_types is None:
            media_types = MediaType.ALL
        media_types_str = "(" + ",".join(f'"{x.value}"' for x in media_types) + ")"

        query = f"""
            SELECT *
            FROM {DB_TABLE_PLAY_HISTORY}
            WHERE userid = :user_id
                AND media_type IN {media_types_str}
        """

        params: dict[str, Any] = {"user_id": user_id}

        if played_after_timestamp is not None:
            query += " AND timestamp >= :played_after_timestamp"
            params["played_after_timestamp"] = played_after_timestamp

        query += " ORDER BY timestamp DESC, id DESC"

        db_rows = await self.mass.music.database.get_rows_from_query(
            query, params, limit=limit, offset=offset
        )

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
            FROM {DB_TABLE_PLAY_HISTORY}
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
            FROM {DB_TABLE_PLAY_HISTORY}
            WHERE userid = :user_id
                AND media_type = :media_type
                AND timestamp >= :since_timestamp
        """

        params = {
            "user_id": user_id,
            "media_type": media_type.value,
            "since_timestamp": since_timestamp,
        }

        played_item_ids: set[tuple[str, str]] = set()
        async for row in self.mass.music.database.iter_rows_from_query(query, params):
            played_item_ids.add((row["provider"], row["item_id"]))
        return played_item_ids

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
