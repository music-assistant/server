"""Statistics Controller implementation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import ItemMapping
from music_assistant_models.media_items.metadata import MediaItemImage
from music_assistant_models.statistics import TopItemResult

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.json import SerializableType, json_loads
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant import MusicAssistant


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

    @api_command("statistics/top_items", required_scope=Scope.LIBRARY_READ)
    async def get_top_items(
        self,
        media_type: MediaType,
        period: str = "week",
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[TopItemResult]:
        """
        Get most played items in a time period.

        Returns list of TopItemResult with 'item' (ItemMapping) and 'play_count' (int).
        Supports ALL MediaTypes: Track, Album, Artist, Radio, Audiobook, Genre, Podcast, etc.

        :param media_type: The type of media to get top items for.
        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param user_id: Optional user ID to filter by. Defaults to current session user.
        :param limit: Maximum number of items to return (default 50).
        """
        if user_id is None:
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
        user = get_current_user()
        user_provider_filter = user.provider_filter if user and user.provider_filter else None

        result: list[TopItemResult] = []

        for row in rows:
            provider = row["provider"]
            if user_provider_filter and provider not in user_provider_filter:
                continue

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

    @api_command("statistics/play_history", required_scope=Scope.LIBRARY_READ)
    async def get_play_history(
        self,
        limit: int = 50,
        media_types: list[MediaType] | None = None,
        userid: str | None = None,
        played_after_timestamp: int | None = None,
    ) -> list[ItemMapping]:
        """
        Get paginated play history with optional time range filtering.

        Delegates to music.recently_played().

        :param limit: Maximum number of items to return (default 50).
        :param media_types: Optional list of media types to filter by.
        :param userid: Optional user ID to filter by. Defaults to current session user.
        :param played_after_timestamp: Optional timestamp to filter items played after.
        """
        return await self.mass.music.recently_played(
            limit=limit,
            media_types=media_types,
            userid=userid,
            played_after_timestamp=played_after_timestamp,
        )

    @api_command("statistics/play_count", required_scope=Scope.LIBRARY_READ)
    async def get_play_count(
        self,
        item_id: str,
        provider: str,
        media_type: MediaType,
        user_id: str | None = None,
    ) -> int:
        """
        Get play count for a specific item.

        :param item_id: The item ID to get play count for.
        :param provider: The provider ID.
        :param media_type: The media type.
        :param user_id: Optional user ID to filter by. Defaults to current session user.
        """
        if user_id is None:
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

    @api_command("statistics/played_item_ids", required_scope=Scope.LIBRARY_READ)
    async def get_played_item_ids(
        self,
        media_type: MediaType,
        user_id: str,
        since_timestamp: float,
    ) -> set[str]:
        """
        Get set of item IDs played since timestamp.

        Used for temporal filters in smart playlists and recommendations.

        :param media_type: The media type to filter by.
        :param user_id: The user ID to filter by.
        :param since_timestamp: Unix timestamp to filter items played after.
        """
        query = f"""
            SELECT DISTINCT item_id
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
        return {row["item_id"] for row in rows}

    def _get_period_cutoff(self, period: str) -> float:
        """
        Get the cutoff timestamp for a time period.

        :param period: Time period string ('today', 'week', 'month', 'year', 'all_time').
        :return: Unix timestamp for the start of the period.
        """
        now = time.time()

        if period == "today":
            # Start of today (midnight) - calculate seconds since midnight
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
