"""Rankings and distributions derived from completed plays."""

from __future__ import annotations

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import ItemMapping
from music_assistant_models.media_items.metadata import MediaItemImage
from music_assistant_models.statistics import (
    DistributionItem,
    TopItemResult,
)

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_PLAY_HISTORY,
    DB_TABLE_PROVIDER_MAPPINGS,
)
from music_assistant.controllers.statistics.base import (
    _StatisticsBase,
    validate_statistics_limit,
)
from music_assistant.controllers.statistics.queries import (
    PROVIDER_MAPPING_JOIN,
    TRACK_ALBUMS_CTE,
    TRACK_ARTISTS_CTE,
    TRACK_HISTORY_CTE,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.json import json_loads


class RankingsMixin(_StatisticsBase):
    """Rankings and distributions derived from completed plays."""

    @api_command("statistics/top_items", required_scope=Scope.LIBRARY_READ)
    async def get_top_items(
        self,
        media_type: MediaType,
        period: str = "week",
        limit: int = 50,
        timezone: str = "UTC",
    ) -> list[TopItemResult]:
        """
        Get most played items in a time period.

        :param media_type: The type of media to get top items for.
        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param limit: Maximum number of items to return (default 50).
        :param timezone: IANA timezone for calendar boundaries; defaults to UTC.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []
        limit = validate_statistics_limit(limit)
        cutoff_timestamp = self._get_period_cutoff(period, timezone)

        if media_type == MediaType.ARTIST:
            query = f"""
                WITH {TRACK_ARTISTS_CTE}
                SELECT
                    item_id,
                    provider,
                    '{MediaType.ARTIST.value}' AS media_type,
                    MAX(name) AS name,
                    MAX(image) AS image,
                    SUM(play_count) AS play_count
                FROM track_artists
                GROUP BY item_id, provider
                ORDER BY play_count DESC, provider, item_id
            """
            params = {"user_id": user_id, "cutoff_timestamp": cutoff_timestamp}
        elif media_type == MediaType.ALBUM:
            query = f"""
                WITH {TRACK_HISTORY_CTE}, {TRACK_ALBUMS_CTE}
                SELECT
                    CAST(item_id AS TEXT) AS item_id,
                    'library' AS provider,
                    '{MediaType.ALBUM.value}' AS media_type,
                    MAX(name) AS name,
                    MAX(image) AS image,
                    SUM(play_count) AS play_count
                FROM track_albums
                GROUP BY item_id
                ORDER BY play_count DESC, item_id
            """
            params = {
                "user_id": user_id,
                "media_type": MediaType.TRACK.value,
                "cutoff_timestamp": cutoff_timestamp,
            }
        else:
            query = f"""
                WITH played_pairs AS (
                    SELECT
                        provider,
                        item_id,
                        media_type,
                        COUNT(*) AS play_count,
                        MAX(name) AS name,
                        MAX(image) AS image
                    FROM {DB_TABLE_PLAY_HISTORY}
                    WHERE userid = :user_id
                        AND media_type = :media_type
                        AND timestamp >= :cutoff_timestamp
                    GROUP BY provider, item_id, media_type
                ), normalized_history AS (
                    SELECT
                        CASE
                            WHEN played_pairs.provider = 'library' THEN played_pairs.item_id
                            WHEN provider_mappings.item_id IS NOT NULL
                                THEN CAST(provider_mappings.item_id AS TEXT)
                            ELSE played_pairs.item_id
                        END AS item_id,
                        CASE
                            WHEN played_pairs.provider = 'library'
                                OR provider_mappings.item_id IS NOT NULL THEN 'library'
                            ELSE played_pairs.provider
                        END AS provider,
                        played_pairs.media_type,
                        played_pairs.play_count,
                        played_pairs.name,
                        played_pairs.image
                    FROM played_pairs
                    LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS provider_mappings
                        ON {
                PROVIDER_MAPPING_JOIN.format(
                    mapping="provider_mappings",
                    media_type="played_pairs.media_type",
                    item_id="played_pairs.item_id",
                    provider="played_pairs.provider",
                )
            }
                )
                SELECT
                    item_id,
                    provider,
                    media_type,
                    MAX(name) AS name,
                    MAX(image) AS image,
                    SUM(play_count) AS play_count
                FROM normalized_history
                GROUP BY item_id, provider, media_type
                ORDER BY play_count DESC, provider, item_id
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
            result.append(TopItemResult(item=item_mapping, play_count=row["play_count"]))

        await self._set_library_item_availability([entry.item for entry in result])
        return result

    @api_command("statistics/genre_distribution", required_scope=Scope.LIBRARY_READ)
    async def get_genre_distribution(
        self,
        period: str = "week",
        limit: int = 10,
        timezone: str = "UTC",
    ) -> list[DistributionItem]:
        """
        Get genre distribution (play counts by genre).

        Counts plays for tracks with known library genre mappings.

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param limit: Maximum number of genres to return (default 10).
        :param timezone: IANA timezone for calendar boundaries; defaults to UTC.
        """
        user = get_current_user()
        if not user:
            return []

        limit = validate_statistics_limit(limit)
        query = f"""
            WITH {TRACK_HISTORY_CTE}
            SELECT genres.name, SUM(history_tracks.play_count) as play_count
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
            "cutoff_timestamp": self._get_period_cutoff(period, timezone),
        }
        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=limit)

        return [DistributionItem(name=row["name"], value=row["play_count"]) for row in rows]

    @api_command("statistics/decade_distribution", required_scope=Scope.LIBRARY_READ)
    async def get_decade_distribution(
        self,
        period: str = "all_time",
        limit: int = 10,
        timezone: str = "UTC",
    ) -> list[DistributionItem]:
        """
        Get play counts grouped by decade for library tracks with known album years.

        Tracks linked to multiple albums use their earliest known album year; provider-only plays
        without a library mapping are omitted.

        :param period: Time period - 'today', 'week', 'month', 'year', 'all_time'.
        :param limit: Maximum number of decades to return.
        :param timezone: IANA timezone for calendar boundaries; defaults to UTC.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return []
        limit = validate_statistics_limit(limit)

        cutoff_timestamp = self._get_period_cutoff(period, timezone)

        query = f"""
            WITH {TRACK_HISTORY_CTE}, track_decades AS (
                SELECT album_tracks.track_id, MIN(albums.year) AS year
                FROM {DB_TABLE_ALBUM_TRACKS} as album_tracks
                INNER JOIN {DB_TABLE_ALBUMS} as albums
                    ON album_tracks.album_id = albums.item_id
                WHERE albums.year > 0
                GROUP BY album_tracks.track_id
            ), history_decades AS (
                SELECT history_tracks.play_count, (track_decades.year / 10) * 10 AS decade
                FROM history_tracks
                INNER JOIN track_decades
                    ON history_tracks.track_id = track_decades.track_id
            )
            SELECT decade, SUM(play_count) as play_count
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

    @api_command("statistics/play_count", required_scope=Scope.LIBRARY_READ)
    async def get_play_count(
        self,
        item_id: str,
        provider: str,
        media_type: MediaType,
    ) -> int:
        """
        Get play count for an item, using track plays for albums and credits for artists.

        :param item_id: The item ID to get play count for.
        :param provider: The provider ID.
        :param media_type: The media type.
        """
        user = get_current_user()
        user_id = user.user_id if user else None

        if not user_id:
            return 0

        if media_type == MediaType.ALBUM:
            history_cte = f"""
                {TRACK_HISTORY_CTE}, {TRACK_ALBUMS_CTE}, normalized_history AS (
                    SELECT
                        CAST(item_id AS TEXT) AS item_id,
                        'library' AS provider,
                        SUM(play_count) AS play_count
                    FROM track_albums
                    GROUP BY item_id
                )
            """
        elif media_type == MediaType.ARTIST:
            history_cte = f"""
                {TRACK_ARTISTS_CTE}, normalized_history AS (
                    SELECT item_id, provider, SUM(play_count) AS play_count
                    FROM track_artists
                    GROUP BY item_id, provider
                )
            """
        else:
            history_cte = f"""
                candidate_keys AS (
                    SELECT item_id, provider FROM requested_item
                    UNION
                    SELECT mappings.provider_item_id, mappings.provider_instance
                    FROM requested_item
                    INNER JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS mappings
                        ON mappings.media_type = :media_type
                        AND mappings.item_id = requested_item.item_id
                    WHERE requested_item.provider = 'library'
                    UNION
                    SELECT mappings.provider_item_id, mappings.provider_domain
                    FROM requested_item
                    INNER JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS mappings
                        ON mappings.media_type = :media_type
                        AND mappings.item_id = requested_item.item_id
                    WHERE requested_item.provider = 'library'
                ), played_pairs AS (
                    SELECT
                        play_history.provider,
                        play_history.item_id,
                        play_history.media_type,
                        COUNT(*) AS play_count
                    FROM candidate_keys
                    INNER JOIN {DB_TABLE_PLAY_HISTORY} AS play_history
                        ON play_history.provider = candidate_keys.provider
                        AND play_history.item_id = candidate_keys.item_id
                    WHERE play_history.userid = :user_id
                        AND play_history.media_type = :media_type
                    GROUP BY play_history.provider, play_history.item_id, play_history.media_type
                ), normalized_history AS (
                    SELECT
                        COALESCE(
                            CAST(provider_mappings.item_id AS TEXT), played_pairs.item_id
                        ) AS item_id,
                        CASE
                            WHEN provider_mappings.item_id IS NOT NULL THEN 'library'
                            ELSE played_pairs.provider
                        END AS provider,
                        SUM(played_pairs.play_count) AS play_count
                    FROM played_pairs
                    LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS provider_mappings
                        ON {
                PROVIDER_MAPPING_JOIN.format(
                    mapping="provider_mappings",
                    media_type="played_pairs.media_type",
                    item_id="played_pairs.item_id",
                    provider="played_pairs.provider",
                )
            }
                                GROUP BY
                                    COALESCE(
                                        CAST(provider_mappings.item_id AS TEXT), played_pairs.item_id
                                    ),
                                    CASE
                                        WHEN provider_mappings.item_id IS NOT NULL THEN 'library'
                                        ELSE played_pairs.provider
                                    END
                )
            """

        # Resolve the request as well as history so a provider URI and the library
        # item shown in rankings have the same count.
        query = f"""
            WITH requested_item AS (
                SELECT
                    COALESCE(CAST(provider_mappings.item_id AS TEXT), :item_id) AS item_id,
                    CASE
                        WHEN provider_mappings.item_id IS NOT NULL THEN 'library'
                        ELSE :provider
                    END AS provider
                FROM (SELECT 1) AS requested
                LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS provider_mappings
                    ON {
            PROVIDER_MAPPING_JOIN.format(
                mapping="provider_mappings",
                media_type=":requested_media_type",
                item_id=":item_id",
                provider=":provider",
            )
        }
            ), {history_cte}
            SELECT COALESCE(SUM(play_count), 0) AS count
            FROM normalized_history
            INNER JOIN requested_item USING (item_id, provider)
        """

        params = {
            "user_id": user_id,
            "item_id": item_id,
            "provider": provider,
            "requested_media_type": media_type.value,
            "media_type": (
                MediaType.TRACK.value if media_type == MediaType.ALBUM else media_type.value
            ),
            "cutoff_timestamp": 0,
        }

        rows = await self.mass.music.database.get_rows_from_query(query, params, limit=1)
        return int(rows[0]["count"]) if rows else 0
