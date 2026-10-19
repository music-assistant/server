"""Completed-play history and recency membership queries."""

from __future__ import annotations

from typing import Any

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import ItemMapping

from music_assistant.constants import (
    DB_TABLE_PLAY_HISTORY,
)
from music_assistant.controllers.statistics.base import (
    MAX_STATISTICS_RESULT_LIMIT,
    _StatisticsBase,
)
from music_assistant.controllers.statistics.cache import cache_statistics
from music_assistant.controllers.webserver.helpers import auth_middleware
from music_assistant.helpers.api import api_command
from music_assistant.helpers.json import json_loads


class HistoryMixin(_StatisticsBase):
    """Completed-play history and recency membership queries."""

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
        user = auth_middleware.get_current_user()
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

        result = [
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
        await self._set_library_item_availability(result)
        return result

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
        user = auth_middleware.get_current_user()
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
