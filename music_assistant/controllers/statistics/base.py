"""Shared state, validation and library/timezone helpers for statistics."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.datetime import get_timezone
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.media_items import ItemMapping

MAX_STATISTICS_RESULT_LIMIT = 500


def validate_statistics_limit(limit: int) -> int:
    """Validate and cap the number of statistics results."""
    if limit < 1:
        raise InvalidDataError("Statistics limit must be at least 1")
    return min(limit, MAX_STATISTICS_RESULT_LIMIT)


class _StatisticsBase(CoreController):
    """Shared state and helpers for statistics logic mixins."""

    domain: str = "statistics"
    _statistics_user_cache_generations: dict[str, str]

    async def _set_library_item_availability(self, items: list[ItemMapping]) -> None:
        """Apply current library availability to historical item references."""
        library_items: dict[MediaType, list[ItemMapping]] = {}
        for item in items:
            if item.provider == "library":
                item.available = False
                library_items.setdefault(item.media_type, []).append(item)

        # History outlives library records; resolve each media type in one batch rather
        # than dropping deleted items or doing one lookup per historical play.
        for media_type, references in library_items.items():
            if media_type not in (
                MediaType.ARTIST,
                MediaType.ALBUM,
                MediaType.TRACK,
                MediaType.PLAYLIST,
                MediaType.RADIO,
                MediaType.AUDIOBOOK,
                MediaType.PODCAST,
                MediaType.GENRE,
            ):
                continue
            controller = self.mass.music.get_controller(media_type)
            item_ids = sorted({item.item_id for item in references})
            params = {f"item_id_{index}": item_id for index, item_id in enumerate(item_ids)}
            placeholders = ", ".join(f":{key}" for key in params)
            current_items = await controller.get_library_items_by_query(
                extra_query_parts=[f"WHERE {controller.db_table}.item_id IN ({placeholders})"],
                extra_query_params=params,
                limit=0,
                in_library_only=False,
            )
            available_ids = {item.item_id for item in current_items if item.available}
            for item in references:
                item.available = item.item_id in available_ids

    def _get_period_cutoff(self, period: str, timezone: str = "UTC") -> float:
        """
        Get the cutoff timestamp for a time period.

        :param period: Time period string ('today', 'week', 'month', 'year', 'all_time').
        :param timezone: IANA timezone for calendar boundaries; defaults to UTC.
        :return: Unix timestamp for the start of the period.
        """
        zone = get_timezone(timezone)
        now = time.time()

        if period == "today":
            # A clock rollback can repeat midnight; the day starts at its first
            # occurrence, even when the current local time is in the second one.
            return (
                datetime.fromtimestamp(now, UTC)
                .astimezone(zone)
                .replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
                .timestamp()
            )
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
        raise InvalidDataError("period must be today, week, month, year, or all_time")
