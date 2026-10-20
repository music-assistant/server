"""Calendar buckets, listening activity and listening-time statistics."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.statistics import (
    HeatmapPoint,
    ListeningTimeItem,
    TimeSeriesPoint,
)

from music_assistant.constants import (
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_PLAY_HISTORY,
    DB_TABLE_TRACKS,
)
from music_assistant.controllers.statistics.base import (
    _StatisticsBase,
    validate_statistics_limit,
)
from music_assistant.controllers.statistics.cache import cache_statistics
from music_assistant.controllers.statistics.queries import (
    PLAY_HISTORY_EVENT_TYPE_SQL,
    TRACK_ARTISTS_CTE,
    TRACK_HISTORY_CTE,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.datetime import get_timezone


class TimeSeriesMixin(_StatisticsBase):
    """Calendar buckets, listening activity and listening-time statistics."""

    @cache_statistics()
    @api_command("statistics/plays_over_time", required_scope=Scope.LIBRARY_READ)
    async def get_plays_over_time(
        self,
        period: str = "week",
        granularity: str = "day",
        timezone: str = "UTC",
    ) -> list[TimeSeriesPoint]:
        """
        Get play counts over time (time series data).

        :param period: Time period - 'today', 'week', 'month', 'year'.
        :param granularity: Time bucket size - 'hour', 'day', 'week', 'month'.
        :param timezone: IANA timezone for calendar buckets and boundaries; defaults to UTC.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []

        cutoff_timestamp = self._get_period_cutoff(period, timezone)

        # SQLite date formatting based on granularity
        date_formats = {
            "hour": "%Y-%m-%d %H:00:00",
            "day": "%Y-%m-%d",
            "week": "%Y-%m-%d",
            "month": "%Y-%m",
        }
        if granularity not in date_formats:
            raise InvalidDataError("granularity must be hour, day, week, or month")
        date_format = date_formats[granularity]
        if timezone != "UTC":
            buckets: dict[str, int] = {}
            async for local_time, count in self._iter_local_play_counts(
                user_id, cutoff_timestamp, timezone
            ):
                bucket_time = (
                    local_time - timedelta(days=local_time.weekday())
                    if granularity == "week"
                    else local_time
                )
                bucket = bucket_time.strftime(date_format)
                buckets[bucket] = buckets.get(bucket, 0) + count
            return [
                TimeSeriesPoint(timestamp=bucket, value=count)
                for bucket, count in sorted(buckets.items())
            ]
        bucket_expression = f"strftime('{date_format}', datetime(timestamp, 'unixepoch'))"
        if granularity == "week":
            bucket_expression = (
                "date(timestamp, 'unixepoch', "
                "'-' || ((CAST(strftime('%w', timestamp, 'unixepoch') AS INTEGER) + 6) % 7)"
                " || ' days')"
            )

        query = f"""
            SELECT
                {bucket_expression} as time_bucket,
                COUNT(*) as play_count
            FROM {DB_TABLE_PLAY_HISTORY}
            WHERE userid = :user_id
                AND timestamp >= :cutoff_timestamp
                AND media_type IN ({PLAY_HISTORY_EVENT_TYPE_SQL})
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
        timezone: str = "UTC",
    ) -> list[HeatmapPoint]:
        """
        Get listening activity heatmap (plays by hour of day and weekday).

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param timezone: IANA timezone for calendar buckets and boundaries; defaults to UTC.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []

        cutoff_timestamp = self._get_period_cutoff(period, timezone)

        if timezone != "UTC":
            buckets: dict[tuple[int, int], int] = {}
            async for local_time, count in self._iter_local_play_counts(
                user_id, cutoff_timestamp, timezone
            ):
                bucket = (local_time.weekday(), local_time.hour)
                buckets[bucket] = buckets.get(bucket, 0) + count
            return [
                HeatmapPoint(hour=hour, weekday=weekday, value=count)
                for (weekday, hour), count in sorted(buckets.items())
            ]

        query = f"""
            SELECT
                CAST(strftime('%H', datetime(timestamp, 'unixepoch')) AS INTEGER) as hour,
                CAST((CAST(strftime('%w', datetime(timestamp, 'unixepoch')) AS INTEGER) + 6) % 7 AS INTEGER) as weekday,
                COUNT(*) as play_count
            FROM {DB_TABLE_PLAY_HISTORY}
            WHERE userid = :user_id
                AND timestamp >= :cutoff_timestamp
                    AND media_type IN ({PLAY_HISTORY_EVENT_TYPE_SQL})
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
        timezone: str = "UTC",
    ) -> list[ListeningTimeItem]:
        """
        Sum stored durations for completed plays resolvable to library tracks.

        Plays without a library mapping or known duration are omitted. Genre grouping also
        requires library genre mappings.

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param group_by: Group by 'artist' or 'genre' (default 'artist').
        :param limit: Maximum number of items to return (default 10).
        :param timezone: IANA timezone for calendar boundaries; defaults to UTC.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []
        limit = validate_statistics_limit(limit)
        if group_by not in ("artist", "genre"):
            raise InvalidDataError("group_by must be 'artist' or 'genre'")

        cutoff_timestamp = self._get_period_cutoff(period, timezone)

        if group_by == "artist":
            query = f"""
                WITH {TRACK_ARTISTS_CTE}, artist_plays AS (
                    SELECT
                        track_artists.play_id,
                        track_artists.item_id AS artist_item_id,
                        track_artists.provider AS artist_provider,
                        track_artists.name AS name,
                        tracks.duration as duration
                    FROM track_artists
                    INNER JOIN {DB_TABLE_TRACKS} as tracks
                        ON tracks.item_id = track_artists.track_id
                    WHERE tracks.duration > 0
                )
                SELECT
                    artist_item_id,
                    artist_provider,
                    MAX(name) as name,
                    SUM(duration) as total_seconds
                FROM artist_plays
                GROUP BY artist_item_id, artist_provider
                ORDER BY total_seconds DESC, artist_provider, artist_item_id
            """
        else:
            query = f"""
                WITH {TRACK_HISTORY_CTE}, genre_plays AS (
                    SELECT DISTINCT
                        history_tracks.play_id,
                        genres.item_id AS genre_id,
                        genres.name AS name,
                        tracks.duration AS duration
                    FROM history_tracks
                    INNER JOIN {DB_TABLE_TRACKS} AS tracks
                        ON tracks.item_id = history_tracks.track_id
                    INNER JOIN {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} AS genre_mappings
                        ON genre_mappings.media_id = history_tracks.track_id
                        AND genre_mappings.media_type = :media_type
                    INNER JOIN {DB_TABLE_GENRES} AS genres
                        ON genres.item_id = genre_mappings.genre_id
                    WHERE tracks.duration > 0
                )
                SELECT name, SUM(duration) as total_seconds
                FROM genre_plays
                GROUP BY genre_id
                ORDER BY total_seconds DESC, name
            """

        params = {
            "user_id": user_id,
            "media_type": MediaType.TRACK.value,
            "cutoff_timestamp": cutoff_timestamp,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        return [
            ListeningTimeItem(name=row["name"], minutes=round(row["total_seconds"] / 60, 1))
            for row in rows
            if row["total_seconds"] is not None and row["total_seconds"] > 0
        ]

    async def _iter_local_play_counts(
        self, user_id: str, cutoff_timestamp: float, timezone: str
    ) -> AsyncGenerator[tuple[datetime, int]]:
        """Yield completed play counts with their local event time."""
        query = f"""
            SELECT timestamp, COUNT(*) AS play_count
            FROM {DB_TABLE_PLAY_HISTORY}
            WHERE userid = :user_id
                AND timestamp >= :cutoff_timestamp
                AND media_type IN ({PLAY_HISTORY_EVENT_TYPE_SQL})
            GROUP BY timestamp
        """
        zone = get_timezone(timezone)
        async for row in self.mass.music.database.iter_rows_from_query(
            query, {"user_id": user_id, "cutoff_timestamp": cutoff_timestamp}
        ):
            yield datetime.fromtimestamp(row["timestamp"], UTC).astimezone(zone), row["play_count"]
