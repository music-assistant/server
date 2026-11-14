"""Completed-play history and recency membership queries."""

from __future__ import annotations

from typing import Any

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.statistics import PlayHistoryItem

from music_assistant.constants import (
    DB_TABLE_PLAY_HISTORY,
)
from music_assistant.controllers.statistics.base import (
    MAX_STATISTICS_RESULT_LIMIT,
    _StatisticsBase,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.json import json_loads


class HistoryMixin(_StatisticsBase):
    """Completed-play history and recency membership queries."""

    @api_command("statistics/play_history", required_scope=Scope.LIBRARY_READ)
    async def get_play_history(
        self,
        limit: int = 50,
        media_types: list[MediaType] | None = None,
        played_after_timestamp: int | None = None,
        offset: int = 0,
        item_id: str | None = None,
        provider: str | None = None,
        media_type: MediaType | None = None,
        period: str = "all_time",
        timezone: str = "UTC",
    ) -> list[PlayHistoryItem]:
        """
        Get paginated play history with optional time range filtering.

        Shows complete user history without provider availability filtering.

        :param limit: Maximum number of items to return (default 50).
        :param media_types: Optional list of media types to filter by.
        :param played_after_timestamp: Optional timestamp to filter items played after.
        :param offset: Number of matching items to skip.
        :param item_id: Optional track, album or artist ID; requires provider and media_type.
        :param provider: Provider instance or domain of the filtered item.
        :param media_type: Type of the filtered item, not the returned events.
        :param period: Calendar period - 'today', 'week', 'month', 'year', 'all_time'.
        :param timezone: IANA timezone for the period boundaries.
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
        item_filter, item_params = await self._get_item_history_filter(
            item_id, provider, media_type
        )
        cutoff = self._get_period_cutoff(period, timezone)

        if media_types is None:
            media_types = [*MediaType.ALL, MediaType.PODCAST_EPISODE]
        if not media_types:
            return []
        media_types_str = "(" + ",".join(f'"{x.value}"' for x in media_types) + ")"

        query = f"""
            SELECT *
            FROM {DB_TABLE_PLAY_HISTORY} AS history
            WHERE userid = :user_id
                AND media_type IN {media_types_str}
                AND timestamp >= :cutoff_timestamp
                {item_filter}
        """

        params: dict[str, Any] = {"user_id": user_id, "cutoff_timestamp": cutoff, **item_params}

        if played_after_timestamp is not None:
            query += " AND timestamp >= :played_after_timestamp"
            params["played_after_timestamp"] = played_after_timestamp

        query += " ORDER BY timestamp DESC, id DESC"

        db_rows = await self.mass.music.database.get_rows_from_query(
            query, params, limit=limit, offset=offset
        )

        available_providers = ("library", *get_global_cache_value("available_providers", []))

        result = [
            PlayHistoryItem.from_dict(
                {
                    "item_id": db_row["item_id"],
                    "provider": db_row["provider"],
                    "media_type": db_row["media_type"],
                    "name": db_row["name"],
                    "image": json_loads(db_row["image"]) if db_row["image"] else None,
                    "available": db_row["provider"] in available_providers,
                    "event_id": db_row["id"],
                    "played_at": db_row["timestamp"],
                    "artists": json_loads(db_row["artists"]) if db_row["artists"] else [],
                }
            )
            for db_row in db_rows
        ]
        await self._set_library_item_availability(result)
        return result
