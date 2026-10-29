"""Calendar buckets, listening activity and listening-time statistics."""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import ItemMapping
from music_assistant_models.media_items.metadata import MediaItemImage
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
from music_assistant.controllers.statistics.queries import (
    PLAY_HISTORY_EVENT_TYPE_SQL,
    TRACK_ARTISTS_CTE,
    TRACK_HISTORY_CTE,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.datetime import get_timezone
from music_assistant.helpers.json import json_loads

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

TIME_SERIES_DATE_FORMATS = {
    "hour": "%Y-%m-%d %H:00:00",
    "day": "%Y-%m-%d",
    "week": "%Y-%m-%d",
    "month": "%Y-%m",
}
# Every UTC offset, and every daylight-saving shift, is a multiple of 15 minutes.
HOUR_BUCKET_SCAN_STEP = timedelta(minutes=15)
# Longer than any daylight-saving shift (Antarctica/Troll moves two hours), taken off in UTC
# so the hour scan starts before the first bucket even when its local start is in a gap.
HOUR_BUCKET_SCAN_MARGIN = timedelta(hours=3)


class TimeSeriesMixin(_StatisticsBase):
    """Calendar buckets, listening activity and listening-time statistics."""

    @api_command("statistics/plays_over_time", required_scope=Scope.LIBRARY_READ)
    async def get_plays_over_time(
        self,
        period: str = "week",
        granularity: str = "day",
        timezone: str = "UTC",
    ) -> list[TimeSeriesPoint]:
        """
        Get play counts over time (time series data).

        The series is complete: every bucket from the start of the period (or the first play,
        for 'all_time') through the current bucket is returned oldest first, with a value of 0
        for buckets without plays. When the period holds no plays at all, an empty list is
        returned.

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param granularity: Time bucket size - 'hour', 'day', 'week', 'month'.
        :param timezone: IANA timezone for calendar buckets and boundaries; defaults to UTC.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []

        cutoff_timestamp = self._get_period_cutoff(period, timezone)
        now_timestamp = time.time()

        if granularity not in TIME_SERIES_DATE_FORMATS:
            raise InvalidDataError("granularity must be hour, day, week, or month")
        date_format = TIME_SERIES_DATE_FORMATS[granularity]
        zone = get_timezone(timezone)
        buckets: dict[str, int] = {}
        if timezone != "UTC":
            async for local_time, count in self._iter_local_play_counts(
                user_id, cutoff_timestamp, timezone
            ):
                bucket = _bucket_key(local_time, granularity)
                buckets[bucket] = buckets.get(bucket, 0) + count
        else:
            bucket_seconds = 3600 if granularity == "hour" else 86400
            bucket_expression = f"strftime('{date_format}', bucket * {bucket_seconds}, 'unixepoch')"
            if granularity == "week":
                bucket_expression = "date(((bucket + 3) / 7 * 7 - 3) * 86400, 'unixepoch')"

            query = f"""
                SELECT
                    {bucket_expression} AS time_bucket,
                    SUM(play_count) AS play_count
                FROM (
                    SELECT CAST(timestamp AS INTEGER) / {bucket_seconds} AS bucket,
                        COUNT(*) AS play_count
                    FROM {DB_TABLE_PLAY_HISTORY}
                    WHERE userid = :user_id
                        AND timestamp >= :cutoff_timestamp
                        AND media_type IN ({PLAY_HISTORY_EVENT_TYPE_SQL})
                    GROUP BY bucket
                )
                GROUP BY time_bucket
                ORDER BY time_bucket ASC
            """

            params = {
                "user_id": user_id,
                "cutoff_timestamp": cutoff_timestamp,
            }

            rows = await self.mass.music.database.get_rows_from_query(query, params, limit=0)
            buckets = {row["time_bucket"]: row["play_count"] for row in rows}

        if not buckets:
            return []
        first_bucket = min(buckets)
        if period != "all_time":
            period_start = datetime.fromtimestamp(cutoff_timestamp, UTC).astimezone(zone)
            first_bucket = min(first_bucket, _bucket_key(period_start, granularity))
        current_time = datetime.fromtimestamp(now_timestamp, UTC).astimezone(zone)
        # Plays stamped after the current bucket (clock skew) still extend the series.
        last_bucket = max(*buckets, _bucket_key(current_time, granularity))
        return [
            TimeSeriesPoint(timestamp=bucket, value=buckets.get(bucket, 0))
            for bucket in _iter_bucket_keys(first_bucket, last_bucket, granularity, zone)
        ]

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
                bucket % 24 AS hour,
                (bucket / 24 + 3) % 7 AS weekday,
                SUM(play_count) AS play_count
            FROM (
                SELECT CAST(timestamp AS INTEGER) / 3600 AS bucket,
                    COUNT(*) AS play_count
                FROM {DB_TABLE_PLAY_HISTORY}
                WHERE userid = :user_id
                    AND timestamp >= :cutoff_timestamp
                    AND media_type IN ({PLAY_HISTORY_EVENT_TYPE_SQL})
                GROUP BY bucket
            )
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
        requires library genre mappings. Artist results carry the artist in `item`, so
        same-named artists stay separate; genre results have no `item`.

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
                        track_artists.play_count,
                        track_artists.item_id AS artist_item_id,
                        track_artists.provider AS artist_provider,
                        track_artists.name AS name,
                        track_artists.image AS image,
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
                    MAX(image) as image,
                    SUM(duration * play_count) as total_seconds
                FROM artist_plays
                GROUP BY artist_item_id, artist_provider
                ORDER BY total_seconds DESC, artist_provider, artist_item_id
            """
        else:
            query = f"""
                WITH {TRACK_HISTORY_CTE}, genre_plays AS (
                    SELECT
                        history_tracks.play_count,
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
                SELECT name, SUM(duration * play_count) as total_seconds
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
        available_providers = ("library", *get_global_cache_value("available_providers", []))
        result: list[ListeningTimeItem] = []
        for row in rows:
            if row["total_seconds"] is None or row["total_seconds"] <= 0:
                continue
            item_mapping = None
            if group_by == "artist":
                image = None
                if row["image"] and (image_dict := json_loads(row["image"])):
                    image = MediaItemImage.from_dict(image_dict)
                item_mapping = ItemMapping(
                    item_id=row["artist_item_id"],
                    provider=row["artist_provider"],
                    media_type=MediaType.ARTIST,
                    name=row["name"],
                    image=image,
                    available=row["artist_provider"] in available_providers,
                )
            result.append(
                ListeningTimeItem(
                    name=row["name"],
                    minutes=round(row["total_seconds"] / 60, 1),
                    item=item_mapping,
                )
            )

        await self._set_library_item_availability(
            [entry.item for entry in result if entry.item is not None]
        )
        return result

    async def _iter_local_play_counts(
        self, user_id: str, cutoff_timestamp: float, timezone: str
    ) -> AsyncGenerator[tuple[datetime, int]]:
        """Yield completed play counts with their local event time."""
        # Recorded-history zone offsets align to 15-minute UTC buckets; grouping first keeps
        # the per-play conversion out of Python and preserves local hour/day boundaries.
        query = f"""
            SELECT CAST(timestamp AS INTEGER) / 900 AS bucket, COUNT(*) AS play_count
            FROM {DB_TABLE_PLAY_HISTORY}
            WHERE userid = :user_id
                AND timestamp >= :cutoff_timestamp
                AND media_type IN ({PLAY_HISTORY_EVENT_TYPE_SQL})
            GROUP BY bucket
        """
        zone = get_timezone(timezone)
        async for row in self.mass.music.database.iter_rows_from_query(
            query, {"user_id": user_id, "cutoff_timestamp": cutoff_timestamp}
        ):
            yield (
                datetime.fromtimestamp(row["bucket"] * 900, UTC).astimezone(zone),
                row["play_count"],
            )


def _bucket_key(local_time: datetime, granularity: str) -> str:
    """Return the time-series bucket key of a local time."""
    if granularity == "week":
        local_time -= timedelta(days=local_time.weekday())
    return local_time.strftime(TIME_SERIES_DATE_FORMATS[granularity])


def _iter_bucket_keys(
    first_bucket: str, last_bucket: str, granularity: str, zone: ZoneInfo
) -> Iterator[str]:
    """
    Yield every bucket key from the first through the last bucket, oldest first.

    :param first_bucket: Key of the first bucket.
    :param last_bucket: Key of the last bucket.
    :param granularity: Time bucket size - 'hour', 'day', 'week', 'month'.
    :param zone: Timezone the bucket keys are expressed in.
    """
    if granularity == "hour":
        # Walk real time in steps no offset change can skip, starting well before the first
        # bucket (whose local start may fall in a gap), keeping each local hour that exists at
        # all, even partly, once and in order however far a daylight-saving change rolls back.
        date_format = TIME_SERIES_DATE_FORMATS["hour"]
        first_start = datetime.strptime(first_bucket, date_format).replace(tzinfo=zone)
        current = first_start.astimezone(UTC) - HOUR_BUCKET_SCAN_MARGIN
        previous: str | None = None
        while (bucket := current.astimezone(zone).strftime(date_format)) <= last_bucket:
            if bucket >= first_bucket and (previous is None or bucket > previous):
                yield bucket
                previous = bucket
            current += HOUR_BUCKET_SCAN_STEP
        return
    if granularity == "month":
        year, month = (int(part) for part in first_bucket.split("-"))
        while (bucket := f"{year:04d}-{month:02d}") <= last_bucket:
            yield bucket
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return
    step = timedelta(days=7 if granularity == "week" else 1)
    day = date.fromisoformat(first_bucket)
    while (bucket := day.isoformat()) <= last_bucket:
        yield bucket
        day += step
