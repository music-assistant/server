"""Shared state, validation and library/timezone helpers for statistics."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    UserNotFoundError,
)
from music_assistant_models.helpers import get_global_cache_value

from music_assistant.constants import DB_TABLE_ALBUM_TRACKS, DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.statistics.queries import PROVIDER_MAPPING_JOIN
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_scope,
)
from music_assistant.controllers.webserver.helpers.auth_providers import normalize_username
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

    async def _get_item_history_filter(
        self, item_id: str | None, provider: str | None, media_type: MediaType | None
    ) -> tuple[str, dict[str, str]]:
        """Return a completed-event filter for a track, album or credited artist."""
        if item_id is None and provider is None and media_type is None:
            return "", {}
        if (
            not item_id
            or not provider
            or media_type is None
            or media_type
            not in (
                MediaType.TRACK,
                MediaType.ALBUM,
                MediaType.ARTIST,
            )
        ):
            raise InvalidDataError(
                "Item filters require item_id, provider and track, album or artist"
            )
        if provider != "library":
            rows = await self.mass.music.database.get_rows_from_query(
                f"SELECT mappings.item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} AS mappings "
                f"WHERE {PROVIDER_MAPPING_JOIN.format(mapping='mappings', media_type=':filter_media_type', item_id=':filter_item_id', provider=':filter_provider')}",
                {
                    "filter_media_type": media_type.value,
                    "filter_item_id": item_id,
                    "filter_provider": provider,
                },
                limit=1,
            )
            if rows:
                item_id, provider = str(rows[0]["item_id"]), "library"
        params = {"filter_item_id": item_id, "filter_provider": provider}
        mapping_join = PROVIDER_MAPPING_JOIN.format(
            mapping="item_mapping",
            media_type="'track'",
            item_id="history.item_id",
            provider="history.provider",
        )
        track_identity = """
            CASE WHEN history.provider = 'library' THEN history.item_id
                ELSE CAST(item_mapping.item_id AS TEXT) END
        """
        if media_type == MediaType.TRACK:
            return (
                f"""
                AND (
                    (history.media_type = 'track' AND history.item_id = :filter_item_id
                        AND history.provider = :filter_provider)
                    OR (:filter_provider = 'library' AND history.media_type = 'track'
                        AND EXISTS (
                            SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} AS item_mapping
                            WHERE {mapping_join}
                                AND CAST(item_mapping.item_id AS TEXT) = :filter_item_id
                        ))
                )
            """,
                params,
            )
        if media_type == MediaType.ALBUM:
            return (
                f"""
                AND history.media_type = 'track'
                AND :filter_provider = 'library'
                AND EXISTS (
                    SELECT 1 FROM (SELECT 1) AS reference
                    LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS item_mapping ON {mapping_join}
                    INNER JOIN {DB_TABLE_ALBUM_TRACKS} AS album_tracks
                        ON CAST(album_tracks.track_id AS TEXT) = {track_identity}
                    WHERE CAST(album_tracks.album_id AS TEXT) = :filter_item_id
                )
            """,
                params,
            )
        artist_id = "json_extract(artist_data.value, '$.item_id')"
        artist_provider = "json_extract(artist_data.value, '$.provider')"
        artist_mapping_join = PROVIDER_MAPPING_JOIN.format(
            mapping="artist_mapping",
            media_type="'artist'",
            item_id=artist_id,
            provider=artist_provider,
        )
        return (
            f"""
            AND history.media_type = 'track'
            AND EXISTS (
                SELECT 1 FROM json_each(history.artists) AS artist_data
                LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} AS artist_mapping ON {artist_mapping_join}
                WHERE (
                    CASE WHEN {artist_provider} = 'library' THEN {artist_id}
                        ELSE COALESCE(CAST(artist_mapping.item_id AS TEXT), {artist_id}) END
                ) = :filter_item_id
                AND (
                    CASE WHEN {artist_provider} = 'library' OR artist_mapping.item_id IS NOT NULL
                        THEN 'library' ELSE {artist_provider} END
                ) = :filter_provider
            )
        """,
            params,
        )

    async def _get_statistics_user_ids(self, users: list[str] | None, all_users: bool) -> list[str]:
        current_user = get_current_user()
        if current_user is None:
            return []
        if users is None and not all_users:
            return [current_user.user_id]
        users = users or []
        # checked before any lookup, so a member can not probe which usernames exist
        if not has_scope(current_user, Scope.USERS_IMPERSONATE):
            if all_users or any(
                user != current_user.user_id and normalize_username(user) != current_user.username
                for user in users
            ):
                raise InsufficientPermissions(
                    "The users.impersonate scope is required for statistics of other users."
                )
            return [current_user.user_id] if users else []
        auth = self.mass.webserver.auth
        if all_users:
            return [user.user_id for user in await auth.list_users()]
        user_ids = []
        for user in users:
            # disabled users keep their play history, and auth/users lists them too
            resolved = await auth.get_user(
                user, include_disabled=True
            ) or await auth.get_user_by_username(user, include_disabled=True)
            if resolved is None:
                raise UserNotFoundError(
                    f"A user with user id or name {user} is not available.",
                    translation_args=[user],
                )
            user_ids.append(resolved.user_id)
        return user_ids

    async def _set_library_item_availability(self, items: Sequence[ItemMapping]) -> None:
        """Apply current library availability to historical item references."""
        library_items: dict[MediaType, list[ItemMapping]] = {}
        provider_items: dict[MediaType, list[ItemMapping]] = {}
        for item in items:
            if item.provider == "library":
                item.available = False
                library_items.setdefault(item.media_type, []).append(item)
            else:
                provider_items.setdefault(item.media_type, []).append(item)

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

        for media_type, references in provider_items.items():
            available_providers = get_global_cache_value("available_providers") or set()
            provider_item_ids = sorted({item.item_id for item in references})
            provider_params: dict[str, str] = {"media_type": media_type.value}
            provider_placeholders: list[str] = []
            for index, item_id in enumerate(provider_item_ids):
                key = f"provider_item_id_{index}"
                provider_params[key] = item_id
                provider_placeholders.append(f":{key}")
            mappings = await self.mass.music.database.get_rows_from_query(
                f"SELECT provider_item_id, provider_instance, provider_domain, available "
                f"FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                f"WHERE media_type = :media_type "
                f"AND provider_item_id IN ({', '.join(provider_placeholders)}) "
                "ORDER BY provider_item_id, item_id, provider_instance",
                provider_params,
                limit=0,
            )
            mappings_by_item: dict[str, list[Mapping[str, Any]]] = {}
            for mapping in mappings:
                mappings_by_item.setdefault(mapping["provider_item_id"], []).append(mapping)
            for item in references:
                item_mappings = mappings_by_item.get(item.item_id, [])
                instance_mapping = next(
                    (
                        entry
                        for entry in item_mappings
                        if entry["provider_instance"] == item.provider
                    ),
                    None,
                )
                if instance_mapping is not None:
                    item.available = item.available and bool(instance_mapping["available"])
                    continue
                domain_mappings = [
                    entry for entry in item_mappings if entry["provider_domain"] == item.provider
                ]
                if domain_mappings:
                    item.available = item.available and any(
                        bool(mapping["available"])
                        and mapping["provider_instance"] in available_providers
                        for mapping in domain_mappings
                    )

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
