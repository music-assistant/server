"""Manage MediaItems of type Genre."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.background_task import BackgroundTask, TaskSchedule
from music_assistant_models.enums import EventType, ImageType, MediaType, TaskStatus
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    GenreSummary,
    MediaItemImage,
    MediaItemMetadata,
    RecommendationFolder,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PODCASTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_RADIOS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
    DEFAULT_AUDIOBOOK_GENRE_MAPPING,
    DEFAULT_GENRE_MAPPING,
    DEFAULT_PODCAST_GENRE_MAPPING,
    GENRE_ICONS_DIR_NAME,
    RESOURCES_DIR,
)
from music_assistant.controllers.music.helpers import search_name_match_clause
from music_assistant.controllers.tasks.context import update_current_task_progress_text
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.datetime import local_clock_time_to_utc
from music_assistant.helpers.json import json_loads, serialize_to_json

from .base import MediaControllerBase

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.event import MassEvent

    from music_assistant import MusicAssistant


MEDIA_TABLES: tuple[tuple[str, MediaType], ...] = (
    (DB_TABLE_TRACKS, MediaType.TRACK),
    (DB_TABLE_ALBUMS, MediaType.ALBUM),
    (DB_TABLE_ARTISTS, MediaType.ARTIST),
    (DB_TABLE_PLAYLISTS, MediaType.PLAYLIST),
    (DB_TABLE_RADIOS, MediaType.RADIO),
    (DB_TABLE_AUDIOBOOKS, MediaType.AUDIOBOOK),
    (DB_TABLE_PODCASTS, MediaType.PODCAST),
)

# Genre taxonomy buckets: a genre content_type (None = music/general) and the media tables
# whose items belong to that taxonomy. Genre resolution and creation are scoped per bucket so
# a podcast "Comedy" never resolves onto (or merges with) the music "Comedy" genre.
GENRE_BUCKETS: tuple[tuple[MediaType | None, tuple[tuple[str, MediaType], ...]], ...] = (
    (
        None,
        (
            (DB_TABLE_TRACKS, MediaType.TRACK),
            (DB_TABLE_ALBUMS, MediaType.ALBUM),
            (DB_TABLE_ARTISTS, MediaType.ARTIST),
            (DB_TABLE_PLAYLISTS, MediaType.PLAYLIST),
            (DB_TABLE_RADIOS, MediaType.RADIO),
        ),
    ),
    (MediaType.AUDIOBOOK, ((DB_TABLE_AUDIOBOOKS, MediaType.AUDIOBOOK),)),
    (MediaType.PODCAST, ((DB_TABLE_PODCASTS, MediaType.PODCAST),)),
)
GENRE_SCAN_TASK_ID = "genre_mapping_scan"

# lifetime of the cached per-taxonomy genre lookup used by sync_media_item_genres;
# kept short so user edits to genres/aliases are picked up quickly by a running sync
SYNC_GENRE_LOOKUP_TTL = 5.0


@dataclass(slots=True)
class _SyncGenreLookup:
    """In-memory snapshot of a genre taxonomy for fast name -> genre_ids resolution."""

    built_at: float
    primary_name_to_genre: dict[str, int]
    alias_to_genre: dict[str, list[int]]
    excluded_names: set[str]


# Curated default genres per taxonomy: (content_type, mapping). Music keeps content_type None;
# podcast/audiobook seed their own namespaced default genres (iTunes / Audible-style lists).
DEFAULT_GENRE_TAXONOMIES: tuple[tuple[MediaType | None, list[dict[str, Any]]], ...] = (
    (None, DEFAULT_GENRE_MAPPING),
    (MediaType.PODCAST, DEFAULT_PODCAST_GENRE_MAPPING),
    (MediaType.AUDIOBOOK, DEFAULT_AUDIOBOOK_GENRE_MAPPING),
)


def genre_content_type_for(media_type: MediaType) -> MediaType | None:
    """Return the genre taxonomy (content_type) a given media type belongs to (None = music)."""
    if media_type == MediaType.AUDIOBOOK:
        return MediaType.AUDIOBOOK
    if media_type in (MediaType.PODCAST, MediaType.PODCAST_EPISODE):
        return MediaType.PODCAST
    return None


class GenreController(MediaControllerBase[Genre]):
    """Controller for Genre entities."""

    db_table = DB_TABLE_GENRES
    media_type = MediaType.GENRE
    item_cls = Genre
    summary_item_cls = GenreSummary

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        self._last_scan_time: float = 0
        self._last_scan_mapped: int = 0
        self._sync_lookup_cache: dict[str | None, _SyncGenreLookup] = {}

        # register extra api handlers
        self.mass.register_api_command(
            "music/genres/add_alias", self.add_alias, required_scope=Scope.LIBRARY_MANAGE
        )
        self.mass.register_api_command(
            "music/genres/remove_alias", self.remove_alias, required_scope=Scope.LIBRARY_MANAGE
        )
        self.mass.register_api_command(
            "music/genres/add_media_mapping",
            self.add_media_mapping,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            "music/genres/remove_media_mapping",
            self.remove_media_mapping,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            "music/genres/promote_alias",
            self.promote_alias_to_genre,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            "music/genres/restore_defaults",
            self.restore_default_genres,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            "music/genres/add",
            self.add_item_to_library,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            "music/genres/overview",
            self.get_overview,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/genres/tracks",
            self.tracks,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/genres/albums",
            self.albums,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/genres/scan_mappings",
            self.scan_mappings,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            "music/genres/scanner_status",
            self.get_scanner_status,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/genres/genres_for_media_item",
            self.get_genres_for_media_item,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/genres/genre_exclusions_for_media_item",
            self.get_genre_exclusions_for_media_item,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/genres/exclude_genre_from_media_item",
            self.exclude_genre_from_media_item,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            "music/genres/remove_genre_exclusion",
            self.remove_genre_exclusion,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            "music/genres/merge",
            self.merge_genres,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            "music/genres/media_counts",
            self.get_genre_media_counts,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/genres/global_exclusions",
            self.get_global_genre_exclusions,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            "music/genres/remove_global_exclusion",
            self.remove_global_genre_exclusion,
            required_scope=Scope.LIBRARY_MANAGE,
        )

        # Run genre mapping scanner after library sync completes
        self.mass.subscribe(self._on_music_sync_completed, EventType.MUSIC_SYNC_COMPLETED)

    @property
    def base_query(self) -> tuple[str, dict[str, Any]]:
        """Return the base SELECT query for genres and its bound query params."""
        # Use a derived table to filter out globally excluded genres so all queries
        # built by the base class (which appends its own WHERE) stay valid SQL.
        query = f"""
        SELECT
            {DB_TABLE_GENRES}.*,
            {self._external_ids_query()} AS external_ids,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                    'item_id', provider_mappings.provider_item_id,
                    'provider_domain', provider_mappings.provider_domain,
                    'provider_instance', provider_mappings.provider_instance,
                    'available', provider_mappings.available,
                    'audio_format', json(provider_mappings.audio_format),
                    'url', provider_mappings.url,
                    'details', provider_mappings.details,
                    'in_library', provider_mappings.in_library,
                    'is_unique', provider_mappings.is_unique
                )) FROM provider_mappings
                WHERE provider_mappings.item_id = {DB_TABLE_GENRES}.item_id
                AND provider_mappings.media_type = '{MediaType.GENRE.value}'
            ) AS provider_mappings
        FROM (SELECT * FROM {DB_TABLE_GENRES} WHERE is_excluded = 0) AS {DB_TABLE_GENRES}"""
        return query, {}

    @property
    def summary_query(self) -> tuple[str, dict[str, Any]]:
        """Return the slim SELECT query used for genre summary listings."""
        # Same derived table as the base query so excluded genres stay hidden.
        query = f"""
        SELECT
            {self._summary_base_columns()},
            {DB_TABLE_GENRES}.translation_key,
            {DB_TABLE_GENRES}.content_type,
            {self._provider_mappings_query()} AS provider_mappings
        FROM (SELECT * FROM {DB_TABLE_GENRES} WHERE is_excluded = 0) AS {DB_TABLE_GENRES}"""
        return query, {}

    async def library_count(self, favorite_only: bool = False) -> int:
        """
        Return the total number of genres in the library.

        Never restricted by the current user's provider filter.

        :param favorite_only: Only count genres marked as favorite.
        """
        # Genres are library-only items without provider_mappings, so - just like
        # library_items below - the user's provider filter does not apply here.
        if favorite_only:
            sql_query = f"SELECT item_id FROM {self.db_table} WHERE favorite = 1"
            return await self.mass.music.database.get_count_from_query(sql_query)
        return await self.mass.music.database.get_count(self.db_table)

    async def library_items(  # noqa: PLR0913
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        played_only: bool = False,
        hide_empty: bool | None = None,
        media_type: MediaType | None = None,
        content_type: str | None = None,
        *,
        summary: bool = True,
        **kwargs: Any,
    ) -> list[Genre]:
        """
        Get genres in the library.

        :param genre: NOT SUPPORTED - Filtering genres by genres doesn't make sense.
        :param hide_empty: Only applies when media_type is not set.
            True: only return genres that have at least one media mapping.
            False: return all genres including unmapped ones.
            None (default): only return default genres (those with a translation_key).
        :param media_type: When set, return all genres (including non-defaults) that have
            at least one mapping for this media type. Takes precedence over hide_empty.
        :param content_type: When set, restrict to genres of one taxonomy: "music" (the
            general/music taxonomy, stored as NULL), "podcast" or "audiobook". Composes with
            hide_empty, so e.g. content_type="podcast" + hide_empty=None returns only the
            default podcast genres.
        :param summary: When True (default), return slim summary items containing only the
            fields needed for a list view. Set to False to get fully hydrated items.
        """
        if genre is not None:
            msg = "genre parameter is not supported for Genre.library_items()"
            raise ValueError(msg)
        # Genres are library-only items without provider_mappings, so ignore
        # the provider filter (the frontend always sends provider="library").
        # Pass raw lowered search for alias matching (search_raw),
        # since the normalized :search param strips spaces/special chars.
        extra_params: dict[str, Any] = {}
        extra_parts: list[str] = []
        if search:
            extra_params["search_raw"] = f"%{search.strip().lower()}%"
        if content_type == "music":
            # the music/general taxonomy is stored as a NULL content_type
            extra_parts.append(f"{self.db_table}.content_type IS NULL")
        elif content_type is not None:
            # restrict to a single taxonomy; composes (AND) with the media_type/hide_empty clause
            extra_parts.append(f"{self.db_table}.content_type IS :filter_content_type")
            extra_params["filter_content_type"] = content_type
        if media_type is not None:
            # media_type implies non-empty: return all genres (including non-default) that
            # have at least one mapping for the requested type.
            gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
            extra_parts.append(
                f"EXISTS(SELECT 1 FROM {gm} gm_mt "
                f"WHERE gm_mt.genre_id = {self.db_table}.item_id "
                "AND gm_mt.media_type = :filter_media_type)"
            )
            extra_params["filter_media_type"] = media_type.value
        elif hide_empty is None:
            extra_parts.append(f"{self.db_table}.translation_key IS NOT NULL")
        elif hide_empty:
            gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
            extra_parts.append(
                f"EXISTS(SELECT 1 FROM {gm} gm WHERE gm.genre_id = {self.db_table}.item_id)"
            )
        items = await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            extra_query_params=extra_params,
            extra_query_parts=extra_parts,
            played_only=played_only,
            summary=summary,
        )
        if kwargs.get("_localized_fallback", True) and search and not items:
            # retry with the canonical name behind a localized query, so genres are findable
            # by the name shown in the user's language (see _localized_search_fallback)
            return await self._localized_search_fallback(
                search,
                limit=limit,
                offset=offset,
                favorite=favorite,
                order_by=order_by,
                played_only=played_only,
                hide_empty=hide_empty,
                media_type=media_type,
                content_type=content_type,
                summary=summary,
            )
        return items

    async def tracks(
        self,
        item_id: str | int,
        limit: int = 500,
        offset: int = 0,
        order_by: str | None = None,
    ) -> list[Track]:
        """
        Return the tracks mapped to a genre.

        :param item_id: The genre's library item ID.
        :param limit: Maximum number of tracks to return (0 = unlimited).
        :param offset: Offset for pagination.
        :param order_by: Sort order (e.g. "random").
        """
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        query = (
            f"EXISTS(SELECT 1 FROM {gm} gm "
            "WHERE gm.media_id = tracks.item_id "
            "AND gm.media_type = 'track' AND gm.genre_id = :genre_id)"
        )
        return await self.mass.music.tracks.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={"genre_id": int(item_id)},
            limit=limit,
            offset=offset,
            order_by=order_by,
        )

    async def albums(
        self,
        item_id: str | int,
        limit: int = 500,
        offset: int = 0,
        order_by: str | None = None,
    ) -> list[Album]:
        """
        Return the albums mapped to a genre.

        :param item_id: The genre's library item ID.
        :param limit: Maximum number of albums to return (0 = unlimited).
        :param offset: Offset for pagination.
        :param order_by: Sort order (e.g. "random").
        """
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        query = (
            f"EXISTS(SELECT 1 FROM {gm} gm "
            "WHERE gm.media_id = albums.item_id "
            "AND gm.media_type = 'album' AND gm.genre_id = :genre_id)"
        )
        return await self.mass.music.albums.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={"genre_id": int(item_id)},
            limit=limit,
            offset=offset,
            order_by=order_by,
        )

    async def mapped_media(
        self,
        item: Genre,
        limit: int = 0,
        offset: int = 0,
        track_limit: int | None = None,
        album_limit: int | None = None,
        artist_limit: int | None = None,
        order_by: str | None = None,
    ) -> tuple[list[Track], list[Album], list[Artist]]:
        """
        Return tracks, albums, and artists mapped to a genre.

        :param item: The genre to fetch mapped media for.
        :param limit: Default limit applied to all media types (0 = unlimited).
        :param offset: Offset for pagination.
        :param track_limit: Override limit for tracks (defaults to limit).
        :param album_limit: Override limit for albums (defaults to limit).
        :param artist_limit: Override limit for artists (defaults to limit).
        :param order_by: Sort order for all queries (e.g. "random").
        """
        db_id = int(item.item_id)
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        t_limit = track_limit if track_limit is not None else limit
        a_limit = album_limit if album_limit is not None else limit
        ar_limit = artist_limit if artist_limit is not None else limit
        artist_query = (
            f"EXISTS(SELECT 1 FROM {gm} gm "
            "WHERE gm.media_id = artists.item_id "
            "AND gm.media_type = 'artist' AND gm.genre_id = :genre_id)"
        )

        tracks, albums, artists = await asyncio.gather(
            self.tracks(db_id, limit=t_limit, offset=offset, order_by=order_by),
            self.albums(db_id, limit=a_limit, offset=offset, order_by=order_by),
            self.mass.music.artists.get_library_items_by_query(
                extra_query_parts=[artist_query],
                extra_query_params={"genre_id": db_id},
                limit=ar_limit,
                offset=offset,
                order_by=order_by,
            ),
        )
        return tracks, albums, artists

    async def get_genres_for_media_item(
        self, media_type: MediaType, media_id: str | int
    ) -> list[Genre]:
        """
        Return all genres mapped to a given media item.

        :param media_type: The type of media item.
        :param media_id: The database ID of the media item.
        """
        try:
            media_id_int = int(media_id)
        except ValueError, TypeError:
            return []
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        query = (
            f"EXISTS(SELECT 1 FROM {gm} gm "
            f"WHERE gm.genre_id = {self.db_table}.item_id "
            "AND gm.media_type = :media_type AND gm.media_id = :media_id)"
        )
        return await self.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={
                "media_type": media_type.value,
                "media_id": media_id_int,
            },
        )

    async def get_genre_exclusions_for_media_item(
        self, media_type: MediaType, media_id: str | int
    ) -> list[Genre]:
        """
        Return all genres excluded from a given media item.

        :param media_type: The type of media item.
        :param media_id: The database ID of the media item.
        """
        try:
            media_id_int = int(media_id)
        except ValueError, TypeError:
            return []
        excl = DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION
        query = (
            f"EXISTS(SELECT 1 FROM {excl} e "
            f"WHERE e.genre_id = {self.db_table}.item_id "
            "AND e.media_type = :media_type AND e.media_id = :media_id)"
        )
        return await self.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={
                "media_type": media_type.value,
                "media_id": media_id_int,
            },
        )

    async def has_derived_genre_mappings(self, media_type: MediaType, media_id: str | int) -> bool:
        """
        Return True if this media item has propagation-derived genre mappings.

        :param media_type: The type of media item.
        :param media_id: The database ID of the media item.
        """
        try:
            media_id_int = int(media_id)
        except ValueError, TypeError:
            return False
        row = await self.mass.music.database.get_row(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {"media_type": media_type.value, "media_id": media_id_int, "is_derived": 1},
        )
        return row is not None

    async def get_overview(
        self,
        item_id: str,
        provider_instance_id_or_domain: str | None = None,
        limit: int = 25,
    ) -> list[RecommendationFolder]:
        """Return overview rows for a genre (all media types)."""
        provider = provider_instance_id_or_domain or "library"
        item = await self.get(item_id, provider)
        db_id = int(item.item_id)
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        media_rows: list[tuple[MediaType, str, str]] = [
            (MediaType.ARTIST, "Artists", "artists"),
            (MediaType.ALBUM, "Albums", "albums"),
            (MediaType.TRACK, "Tracks", "tracks"),
            (MediaType.PLAYLIST, "Playlists", "playlists"),
            (MediaType.RADIO, "Radio", "radios"),
            (MediaType.PODCAST, "Podcasts", "podcasts"),
            (MediaType.AUDIOBOOK, "Audiobooks", "audiobooks"),
        ]

        async def _fetch_media_type(
            media_type: MediaType, title: str, translation_key: str
        ) -> RecommendationFolder | None:
            ctrl = self.mass.music.get_controller(media_type)
            query = (
                f"EXISTS(SELECT 1 FROM {gm} gm "
                f"WHERE gm.media_id = {ctrl.db_table}.item_id "
                "AND gm.media_type = :media_type "
                "AND gm.genre_id = :genre_id)"
            )
            items = await ctrl.get_library_items_by_query(
                extra_query_parts=[query],
                extra_query_params={
                    "genre_id": db_id,
                    "media_type": media_type.value,
                },
                limit=limit,
            )
            if not items:
                return None
            return RecommendationFolder(
                item_id=f"genre_{media_type.value}",
                name=title,
                translation_key=translation_key,
                provider="library",
                items=UniqueList(items[:limit]),
            )

        results = await asyncio.gather(
            *[_fetch_media_type(mt, title, key) for mt, title, key in media_rows]
        )
        return [r for r in results if r is not None]

    async def get_genre_media_counts(self, genre_ids: list[str]) -> dict[str, dict[str, int]]:
        """
        Return media item counts per media type for each requested genre.

        :param genre_ids: List of genre database IDs to query.
        :return: Mapping of genre_id -> {media_type -> count}.
        """
        if not genre_ids:
            return {}
        try:
            int_ids = [int(gid) for gid in genre_ids]
        except (TypeError, ValueError) as err:
            raise InvalidDataError(f"Invalid genre_id value: {err}") from err
        norm_ids = [str(i) for i in int_ids]
        placeholders = ",".join(norm_ids)
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT {gm}.genre_id, {gm}.media_type, COUNT(*) AS cnt "
            f"FROM {gm} "
            f"WHERE {gm}.genre_id IN ({placeholders}) "
            f"AND EXISTS ("
            f"  SELECT 1 FROM provider_mappings pm "
            f"  WHERE pm.item_id = {gm}.media_id "
            f"  AND pm.media_type = {gm}.media_type "
            f"  AND pm.in_library = 1"
            f") "
            f"GROUP BY {gm}.genre_id, {gm}.media_type",
            limit=0,
        )
        empty: dict[str, int] = {mt.value: 0 for _, mt in MEDIA_TABLES}
        result: dict[str, dict[str, int]] = {nid: dict(empty) for nid in norm_ids}
        for row in rows:
            gid = str(row["genre_id"])
            if gid in result:
                result[gid][row["media_type"]] = row["cnt"]
        return result

    async def match_providers(self, db_item: Genre) -> None:
        """No provider matching for genres at this time."""
        return

    async def restore_default_genres(
        self, full_restore: bool = False, content_type: str | None = None
    ) -> list[Genre]:
        """
        Restore default genres for one or every taxonomy (music, podcast, audiobook).

        :param full_restore: If True, delete all existing genres and recreate from defaults
                            (always covers every taxonomy). If False (default), only add
                            missing genres and ensure aliases exist.
        :param content_type: Restrict a non-destructive restore to a single taxonomy:
                            "music", "podcast" or "audiobook". None or "all" restores every
                            taxonomy. Ignored when full_restore is True.
        """
        if full_restore:
            self.logger.warning("Performing FULL restore - deleting all existing genres")
            await self.mass.music.database.delete(DB_TABLE_GENRE_MEDIA_ITEM_MAPPING)
            await self.mass.music.database.delete(DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION)
            await self.mass.music.database.delete(
                DB_TABLE_PLAYLOG, {"media_type": MediaType.GENRE.value}
            )
            await self.mass.music.database.delete(DB_TABLE_GENRES)

        taxonomies = DEFAULT_GENRE_TAXONOMIES
        if not full_restore and content_type is not None and content_type != "all":
            # the music taxonomy is stored as a NULL content_type
            wanted = None if content_type == "music" else MediaType(content_type)
            taxonomies = tuple(t for t in DEFAULT_GENRE_TAXONOMIES if t[0] == wanted)
            if not taxonomies:
                msg = f"Unknown genre taxonomy: {content_type}"
                raise ValueError(msg)

        created_ids: list[int] = []
        for taxonomy_content_type, mapping in taxonomies:
            created_ids.extend(
                await self._seed_default_genres(taxonomy_content_type, mapping, full_restore)
            )

        if created_ids:
            await self.mass.music.database.commit()

        if full_restore:
            await self._bulk_scan_media_genres()

        if not created_ids:
            return []
        return [await self.get_library_item(item_id) for item_id in created_ids]

    async def remove_item_from_library(
        self, item_id: str | int, recursive: bool = True, exclude_globally: bool = True
    ) -> None:
        """
        Delete genre record from the database.

        :param item_id: Database ID of the genre to remove.
        :param recursive: Unused for genres, kept for base-class compatibility.
        :param exclude_globally: If True (default), soft-delete the genre so the scanner
            will not recreate it. If False, hard-delete the row (used internally by
            merge_genres where the source should not appear in the exclusion list).
        """
        db_id = int(item_id)
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING, {"genre_id": db_id}
        )
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION, {"genre_id": db_id}
        )
        if exclude_globally:
            # Fetch the item while it is still visible (base_query hides is_excluded=1 rows).
            library_item = await self.get_library_item(db_id)
            await self.mass.music.database.update(
                DB_TABLE_GENRES, {"item_id": db_id}, {"is_excluded": 1}
            )
            self.mass.signal_event(EventType.MEDIA_ITEM_DELETED, library_item.uri, library_item)
        else:
            await super().remove_item_from_library(item_id, recursive)

    async def add_alias(self, genre_id: str | int, alias: str) -> Genre:
        """
        Add an alias string to a genre.

        :param genre_id: Database ID of the genre.
        :param alias: Alias string to add.
        """
        db_id = int(genre_id)
        genre = await self.get_library_item(db_id)
        aliases = list(genre.genre_aliases) if genre.genre_aliases else []
        aliases = self._dedup_aliases(aliases, [alias])
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {"genre_aliases": serialize_to_json(aliases)},
        )
        updated = await self.get_library_item(db_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, updated.uri, updated)
        return updated

    async def remove_alias(self, genre_id: str | int, alias: str) -> Genre:
        """
        Remove an alias string from a genre.

        :param genre_id: Database ID of the genre.
        :param alias: Alias string to remove.
        :raises ValueError: If trying to remove the genre's own name.
        """
        db_id = int(genre_id)
        genre = await self.get_library_item(db_id)
        if create_safe_string(alias, True, True) == create_safe_string(genre.name, True, True):
            msg = (
                f"Cannot remove self-alias '{alias}' from genre '{genre.name}'. "
                f"Delete the genre instead."
            )
            raise ValueError(msg)
        aliases = list(genre.genre_aliases) if genre.genre_aliases else []
        alias_norm = create_safe_string(alias, True, True)
        aliases = [a for a in aliases if create_safe_string(a, True, True) != alias_norm]
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {"genre_aliases": serialize_to_json(aliases)},
        )
        # Remove media mappings that were created via this alias (case-insensitive)
        await self.mass.music.database.execute(
            f"DELETE FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :genre_id AND LOWER(alias) = LOWER(:alias)",
            {"genre_id": db_id, "alias": alias},
        )
        # Derived album/artist rows can be left orphaned by the deleted track rows.
        await self._propagate_genre_mappings_to_parents()
        updated = await self.get_library_item(db_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, updated.uri, updated)
        return updated

    async def add_media_mapping(
        self,
        genre_id: str | int,
        media_type: MediaType,
        media_id: str | int,
        alias: str | None = None,
    ) -> None:
        """
        Map a media item to a genre.

        :param genre_id: Database ID of the genre.
        :param media_type: Type of media item (track, album, artist).
        :param media_id: Database ID of the media item.
        :param alias: The alias string that caused this mapping. If not provided,
            the genre's primary name is used.
        """
        if alias is None:
            genre = await self.get_library_item(int(genre_id))
            alias = genre.name
        await self.mass.music.database.insert(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": int(genre_id),
                "media_id": int(media_id),
                "media_type": media_type.value,
                "alias": alias,
                "is_manual": 1,
            },
            allow_replace=True,
        )

    async def remove_media_mapping(
        self, genre_id: str | int, media_type: MediaType, media_id: str | int
    ) -> None:
        """
        Remove a media item mapping from a genre.

        If the mapping was derived (propagated from child tracks), an exclusion is
        automatically inserted so the next propagation scan does not re-derive it.

        :param genre_id: Database ID of the genre.
        :param media_type: Type of media item (track, album, artist).
        :param media_id: Database ID of the media item.
        """
        row = await self.mass.music.database.get_row(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {"genre_id": int(genre_id), "media_id": int(media_id), "media_type": media_type.value},
        )
        if row and row["is_derived"]:
            await self.exclude_genre_from_media_item(genre_id, media_type, media_id)
            return
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": int(genre_id),
                "media_id": int(media_id),
                "media_type": media_type.value,
            },
        )

    async def exclude_genre_from_media_item(
        self,
        genre_id: str | int,
        media_type: MediaType,
        media_id: str | int,
    ) -> None:
        """
        Permanently exclude a genre from being mapped to a media item.

        Records the exclusion so the scanner will never re-add this mapping.
        Any existing mapping for this genre/media pair is removed immediately.

        :param genre_id: Database ID of the genre.
        :param media_type: Type of media item (track, album, artist, etc.).
        :param media_id: Database ID of the media item.
        """
        params = {
            "genre_id": int(genre_id),
            "media_id": int(media_id),
            "media_type": media_type.value,
        }
        db = self.mass.music.database
        # Run both statements without committing between them so the exclusion insert
        # and the mapping delete are committed atomically.
        await db.execute(
            f"INSERT OR REPLACE INTO {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}"
            "(genre_id, media_id, media_type) VALUES (:genre_id, :media_id, :media_type)",
            params,
        )
        await db.execute(
            f"DELETE FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "WHERE genre_id = :genre_id AND media_id = :media_id AND media_type = :media_type",
            params,
        )
        await db.commit()

    async def remove_genre_exclusion(
        self,
        genre_id: str | int,
        media_type: MediaType,
        media_id: str | int,
    ) -> None:
        """
        Remove a genre exclusion, allowing the scanner to re-map it on the next run.

        :param genre_id: Database ID of the genre.
        :param media_type: Type of media item (track, album, artist, etc.).
        :param media_id: Database ID of the media item.
        """
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
            {
                "genre_id": int(genre_id),
                "media_id": int(media_id),
                "media_type": media_type.value,
            },
        )

    async def get_global_genre_exclusions(self) -> list[dict[str, object]]:
        """Return all globally excluded genres."""
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id, name, sort_name, search_name, translation_key, metadata "
            f"FROM {DB_TABLE_GENRES} WHERE is_excluded = 1 ORDER BY sort_name",
            limit=0,
        )
        result = []
        for row in rows:
            entry = dict(row)
            if raw_metadata := entry.get("metadata"):
                entry["metadata"] = json_loads(raw_metadata)
            result.append(entry)
        return result

    async def remove_global_genre_exclusion(self, genre_id: int) -> Genre:
        """
        Lift a global genre exclusion, making the genre visible and scannable again.

        :param genre_id: Database ID of the excluded genre (item_id in genres table).
        :return: The restored Genre.
        """
        row = await self.mass.music.database.get_row(
            DB_TABLE_GENRES, {"item_id": genre_id, "is_excluded": 1}
        )
        if not row:
            msg = f"No globally excluded genre found with id {genre_id}"
            raise KeyError(msg)
        await self.mass.music.database.update(
            DB_TABLE_GENRES, {"item_id": genre_id}, {"is_excluded": 0}
        )
        library_item = await self.get_library_item(genre_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_ADDED, library_item.uri, library_item)
        return library_item

    async def promote_alias_to_genre(self, genre_id: str | int, alias: str) -> Genre:
        """
        Promote an alias to become a standalone genre.

        Every genre that claimed the alias loses it, and all media mapped via
        the alias is moved to the new genre.

        :param genre_id: Database ID of the source genre.
        :param alias: The alias string to promote.
        :return: The newly created Genre.
        """
        db_genre_id = int(genre_id)
        source_genre = await self.get_library_item(db_genre_id)
        alias_norm = create_safe_string(alias, True, True)

        if alias_norm == create_safe_string(source_genre.name, True, True):
            msg = (
                f"Cannot promote self-alias '{alias}'. "
                f"This alias is the primary name for genre '{source_genre.name}'."
            )
            raise ValueError(msg)

        owning_ids = await self._find_genre_ids_for_alias(alias_norm)
        if db_genre_id not in owning_ids:
            owning_ids.append(db_genre_id)

        new_genre = Genre(
            item_id="0",
            provider="library",
            name=alias,
            sort_name=alias,
            translation_key=None,
            provider_mappings=set(),
            favorite=False,
            # the promoted genre stays in the same taxonomy as the genre it came from
            content_type=source_genre.content_type,
        )
        created_genre = await self.add_item_to_library(new_genre)
        new_genre_id = int(created_genre.item_id)

        # UPDATE OR REPLACE drops any pre-existing mapping on the new genre for
        # the same (media_id, media_type) so the moved row wins.
        placeholders = ", ".join(str(g) for g in owning_ids)
        await self.mass.music.database.execute(
            f"UPDATE OR REPLACE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            f"SET genre_id = :new_id "
            f"WHERE genre_id IN ({placeholders}) AND LOWER(alias) = LOWER(:alias)",
            {"new_id": new_genre_id, "alias": alias},
        )

        for owning_id in owning_ids:
            owning = await self.get_library_item(owning_id)
            # Defensive: a genre whose primary name equals the alias would hit
            # the self-alias guard in remove_alias; skip rather than raise.
            if create_safe_string(owning.name, True, True) == alias_norm:
                continue
            owning_aliases = list(owning.genre_aliases) if owning.genre_aliases else []
            filtered = [
                a for a in owning_aliases if create_safe_string(a, True, True) != alias_norm
            ]
            if len(filtered) != len(owning_aliases):
                await self.mass.music.database.update(
                    self.db_table,
                    {"item_id": owning_id},
                    {"genre_aliases": serialize_to_json(filtered)},
                )

        # Derived album/artist rows still point at the old source genres; rebuild
        # them from the moved track mappings.
        await self._propagate_genre_mappings_to_parents()

        return await self.get_library_item(new_genre_id)

    async def merge_genres(self, genre_ids: list[str | int], target_genre_id: str | int) -> Genre:
        """
        Merge one or more genres into a target genre.

        Transfers all aliases and media mappings from the source genres to the
        target, then deletes the source genres. Aliases and mappings are
        deduplicated so no duplicates are created on the target.

        :param genre_ids: List of genre IDs to merge into the target.
        :param target_genre_id: Database ID of the genre to merge into.
        """
        target_id = int(target_genre_id)
        source_ids = [int(gid) for gid in genre_ids]

        if target_id in source_ids:
            msg = "Target genre cannot be in the list of genres to merge"
            raise ValueError(msg)
        if not source_ids:
            msg = "No genre IDs provided to merge"
            raise ValueError(msg)

        target_genre = await self.get_library_item(target_id)
        db = self.mass.music.database
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING

        # Collect and merge aliases from all source genres into the target. Genres can only be
        # merged within the same taxonomy — merging e.g. a podcast genre into a music genre
        # would attach spoken-word items to a music genre (and be undone by the next scan).
        all_new_aliases: list[str] = []
        for source_id in source_ids:
            source_genre = await self.get_library_item(source_id)
            if source_genre.content_type != target_genre.content_type:
                msg = (
                    f"Cannot merge genre '{source_genre.name}' into '{target_genre.name}': "
                    "genres must belong to the same taxonomy (music / podcast / audiobook)."
                )
                raise ValueError(msg)
            if source_genre.genre_aliases:
                all_new_aliases.extend(source_genre.genre_aliases)

        existing_aliases = list(target_genre.genre_aliases) if target_genre.genre_aliases else []
        merged_aliases = self._dedup_aliases(existing_aliases, all_new_aliases)
        await db.update(
            self.db_table,
            {"item_id": target_id},
            {"genre_aliases": serialize_to_json(merged_aliases)},
        )

        # Transfer media mappings from source genres to target (deduplicated)
        placeholders = ", ".join(str(sid) for sid in source_ids)
        await db.execute(
            f"INSERT OR IGNORE INTO {gm} (genre_id, media_id, media_type, alias) "
            f"SELECT :target_id, media_id, media_type, alias FROM {gm} "
            f"WHERE genre_id IN ({placeholders})",
            {"target_id": target_id},
        )

        # Hard-delete source genres: merging is not a user exclusion so sources must
        # not appear in the global exclusion list.
        for source_id in source_ids:
            await self.remove_item_from_library(source_id, exclude_globally=False)

        # Rebuild derived album/artist rows against the merged track mappings.
        await self._propagate_genre_mappings_to_parents()

        updated = await self.get_library_item(target_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, updated.uri, updated)
        return updated

    async def sync_media_item_genres(
        self, media_type: MediaType, media_id: str | int, genre_names: set[str]
    ) -> None:
        """
        Sync genre mappings for a media item.

        Ensures genre records exist and updates genre-media mappings.
        Removes mappings that are no longer present in the incoming genre_names set.

        :param media_type: The type of media item being synced.
        :param media_id: The database ID of the media item.
        :param genre_names: Set of genre names from the provider.
        """
        media_id_int = int(media_id)
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        content_type = genre_content_type_for(media_type)

        # fast path for the (very common) unchanged case: resolve the incoming names
        # against a short-lived cached snapshot of this taxonomy — the same resolution
        # the full path performs — and skip all writes when the resolved genre ids
        # match the stored mappings exactly. Unknown names require genre creation, so
        # they (and any mismatch) fall through to the full path below.
        target_ids = await self._resolve_genre_names_cached(genre_names, content_type)
        if target_ids is not None:
            stored_rows = await self.mass.music.database.get_rows_from_query(
                f"SELECT DISTINCT genre_id FROM {gm} "
                "WHERE media_type = :media_type AND media_id = :media_id",
                {"media_type": media_type.value, "media_id": media_id_int},
                limit=0,
            )
            if {int(row["genre_id"]) for row in stored_rows} == target_ids:
                return

        # batch the (possible) genre creations and mapping changes into a single commit
        async with self.mass.music.database.deferred_commit():
            # Build target set: (genre_id, alias_name) from incoming names.
            # One alias can map to multiple genres (n:n). Genres resolve within the taxonomy
            # the item belongs to, so a podcast tag never lands on a music genre.
            target_mappings: dict[int, str] = {}
            for name in genre_names:
                normalized = self._normalize_genre_name(name)
                if not normalized:
                    continue
                genre_ids = await self._find_genres_for_alias(normalized[0], content_type)
                for gid in genre_ids:
                    if gid not in target_mappings:
                        target_mappings[gid] = normalized[0]

            # Get current genre_ids from database
            rows = await self.mass.music.database.get_rows_from_query(
                f"SELECT genre_id FROM {gm} "
                "WHERE media_type = :media_type AND media_id = :media_id",
                {"media_type": media_type.value, "media_id": media_id_int},
                limit=0,
            )
            existing_genre_ids = {int(row["genre_id"]) for row in rows}

            to_add = set(target_mappings.keys()) - existing_genre_ids
            to_remove = existing_genre_ids - set(target_mappings.keys())

            for genre_id in to_remove:
                await self.mass.music.database.delete(
                    gm,
                    {
                        "genre_id": genre_id,
                        "media_id": media_id_int,
                        "media_type": media_type.value,
                    },
                )

            for genre_id in to_add:
                await self.mass.music.database.insert(
                    gm,
                    {
                        "genre_id": genre_id,
                        "media_id": media_id_int,
                        "media_type": media_type.value,
                        "alias": target_mappings[genre_id],
                    },
                    allow_replace=True,
                )

    def register_scheduled_scan_task(self) -> BackgroundTask:
        """Register the recurring genre mapping scan task."""
        utc_hour, utc_minute = local_clock_time_to_utc(4, 0)
        desired_schedule = TaskSchedule.daily(hour=utc_hour, minute=utc_minute)
        return self.mass.tasks.register_scheduled_task(
            task_id=GENRE_SCAN_TASK_ID,
            name="Scan genre mappings",
            handler=self._scan_genre_mappings,
            schedule=desired_schedule,
            translation_key="scan_genre_mappings",
            translation_owner=self.translation_owner,
            metadata={
                "task_domain": "genre_mapping_scan",
            },
            allow_retry=True,
        )

    async def scan_mappings(self) -> dict[str, Any]:
        """
        Manually trigger a genre mapping scan (admin only).

        :return: Status information about the scan trigger.
        """
        if self._genre_scan_running:
            return {
                "status": "already_running",
                "message": "Genre mapping scanner is already running",
            }

        self._queue_genre_mapping_scan_task()

        return {
            "status": "triggered",
            "message": "Genre mapping scan triggered",
            "last_scan": self._last_scan_time,
        }

    async def get_scanner_status(self) -> dict[str, Any]:
        """
        Get status of the genre mapping background scanner.

        :return: Scanner status information.
        """
        return {
            "running": self._genre_scan_running,
            "last_scan_time": self._last_scan_time,
            "last_scan_ago_seconds": (
                int(time.time() - self._last_scan_time) if self._last_scan_time else None
            ),
            "last_scan_mapped": self._last_scan_mapped,
        }

    @staticmethod
    def _get_genre_icon_metadata(
        translation_key: str | None, content_type: MediaType | None = None
    ) -> MediaItemMetadata | None:
        """
        Build metadata with the genre icon image if an SVG exists for the translation key.

        Spoken-word taxonomies keep their icons in a per-content_type subdir
        (``genres/podcast/<key>.svg``); the flat ``genres/<key>.svg`` (music, or a
        shared symbol) is used as a fallback.

        :param translation_key: The genre's translation key (matches the SVG filename).
        :param content_type: The genre's taxonomy (None = music/general).
        """
        if not translation_key:
            return None
        # taxonomy-specific icon first, then the flat/shared one
        rel_candidates: list[str] = []
        if content_type is not None:
            rel_candidates.append(f"{content_type.value}/{translation_key}.svg")
        rel_candidates.append(f"{translation_key}.svg")
        for rel in rel_candidates:
            if RESOURCES_DIR.joinpath(GENRE_ICONS_DIR_NAME, rel).is_file():
                image = MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"{GENRE_ICONS_DIR_NAME}/{rel}",
                    provider="builtin",
                )
                return MediaItemMetadata(images=UniqueList([image]))
        return None

    @staticmethod
    def _dedup_aliases(existing: list[str], new: list[str]) -> list[str]:
        """
        Merge alias lists, deduplicating by normalized form (create_safe_string).

        Preserves the first occurrence's original casing.

        :param existing: Current aliases (ordering preserved).
        :param new: New aliases to add if not already present.
        """
        seen: set[str] = set()
        result: list[str] = []
        for alias in [*existing, *new]:
            norm = create_safe_string(alias, True, True)
            if norm and norm not in seen:
                seen.add(norm)
                result.append(alias)
        return result

    def _search_filter_clause(self, search: str, query_params: dict[str, Any]) -> str:
        """Return search filter that also matches genre aliases."""
        name_clause = search_name_match_clause(self.db_table, search, "search", query_params)
        return (
            f"({name_clause}"
            " OR EXISTS("
            f"SELECT 1 FROM json_each({self.db_table}.genre_aliases) "
            "WHERE LOWER(json_each.value) LIKE :search_raw))"
        )

    async def _add_library_item(self, item: Genre, overwrite_existing: bool = False) -> int:
        """Add a new genre record to the database."""
        aliases: list[str] = list(item.genre_aliases) if item.genre_aliases else [item.name]
        # Ensure the genre's own name is always in aliases (normalized comparison)
        name_norm = create_safe_string(item.name, True, True)
        if not any(create_safe_string(a, True, True) == name_norm for a in aliases):
            aliases.insert(0, item.name)
        content_type_value = item.content_type.value if item.content_type else None
        # If a soft-deleted genre with the same name in the same taxonomy exists, restore it
        # instead of inserting (scoped by content_type so a podcast "Comedy" never restores a
        # soft-deleted music "Comedy").
        excl_rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id FROM {DB_TABLE_GENRES} "
            "WHERE search_name = :search_name AND is_excluded = 1 "
            "AND content_type IS :content_type",
            {"search_name": name_norm, "content_type": content_type_value},
            limit=1,
        )
        if excl_rows:
            db_id = int(excl_rows[0]["item_id"])
            await self.mass.music.database.update(
                DB_TABLE_GENRES, {"item_id": db_id}, {"is_excluded": 0}
            )
            self.logger.debug("restored soft-deleted genre %s (id: %s)", item.name, db_id)
            return db_id
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "translation_key": item.translation_key,
                "description": item.metadata.description if item.metadata else None,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "genre_aliases": serialize_to_json(aliases),
                "play_count": 0,
                "last_played": 0,
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": UNSET,
                "is_default": 0,
                "content_type": content_type_value,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(db_id, item.external_ids)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Genre, overwrite: bool = False
    ) -> None:
        """Update existing genre record in the database."""
        db_id = int(item_id)
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        existing_description = await self._get_description(db_id)
        description = (
            update.metadata.description
            if update.metadata and update.metadata.description is not None
            else None
            if overwrite
            else existing_description
        )
        # Merge aliases: keep existing, add any new from update (normalized dedup)
        existing_aliases = list(cur_item.genre_aliases) if cur_item.genre_aliases else []
        update_aliases = list(update.genre_aliases) if update.genre_aliases else []
        if overwrite:
            merged_aliases = self._dedup_aliases(update_aliases, [name])
        else:
            merged_aliases = self._dedup_aliases(existing_aliases, [*update_aliases, name])

        # content_type (the genre's taxonomy) is set at creation and never changed by an edit,
        # so an update — even with overwrite — must not clobber it.
        content_type = cur_item.content_type

        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": name,
                "sort_name": sort_name,
                "translation_key": update.translation_key
                if overwrite
                else cur_item.translation_key,
                "description": description,
                "favorite": update.favorite,
                "metadata": serialize_to_json(metadata),
                "genre_aliases": serialize_to_json(merged_aliases),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": UNSET,
                "content_type": content_type.value if content_type else None,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(
            db_id, update.external_ids if overwrite else cur_item.external_ids
        )
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _bulk_scan_media_genres(self) -> None:
        """
        Bulk-scan all media items and rebuild genre mappings using CTE.

        Resolution is scoped per genre taxonomy (music / audiobook / podcast): for each bucket
        the genre names from that bucket's tables are resolved against — and created within —
        only that taxonomy's genres, then mapped with a single INSERT per media type.
        """
        db = self.mass.music.database
        excl = DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION
        total_resolved = 0

        for content_type, tables in GENRE_BUCKETS:
            # Build alias and primary-name lookups for this taxonomy. Primary-name match takes
            # priority over alias match so a bare "pop" tag only maps to the Pop genre, not every
            # genre that accumulated "pop" as a secondary alias.
            alias_to_genre, primary_name_to_genre = await self._build_genre_lookup(content_type)

            union_parts = [
                f"SELECT DISTINCT TRIM(g.value) AS raw_name "
                f"FROM {table}, "
                f"json_each(json_extract({table}.metadata, '$.genres')) AS g "
                f"WHERE TRIM(g.value) != ''"
                for table, _ in tables
            ]
            unique_names_sql = " UNION ".join(union_parts)
            rows = await db.get_rows_from_query(unique_names_sql, limit=0)
            unique_raw_names = [row["raw_name"] for row in rows if row["raw_name"]]

            # Resolve each raw name to genre_ids within this taxonomy.
            # One raw name can map to multiple genres (n:n), except when a genre's primary name
            # exactly matches the normalised tag — in that case use only that single genre.
            raw_name_to_genres: dict[str, list[int]] = {}
            for raw_name in unique_raw_names:
                norm = create_safe_string(raw_name.strip(), True, True)
                if not norm:
                    continue
                if norm in primary_name_to_genre:
                    raw_name_to_genres[raw_name] = [primary_name_to_genre[norm]]
                elif norm in alias_to_genre:
                    raw_name_to_genres[raw_name] = alias_to_genre[norm]
                else:
                    resolved_ids = await self._find_genres_for_alias(raw_name, content_type)
                    if resolved_ids:
                        raw_name_to_genres[raw_name] = resolved_ids
                        alias_to_genre[norm] = resolved_ids

            total_resolved += len(raw_name_to_genres)

            # Add discovered raw names as aliases to their resolved genres so that future
            # searches by raw name (e.g. "Synthpop") find the parent genre even when the stored
            # alias differs (e.g. "synth-pop").
            genre_new_aliases: dict[int, list[str]] = {}
            for raw_name, gids in raw_name_to_genres.items():
                for gid in gids:
                    genre_new_aliases.setdefault(gid, []).append(raw_name)
            for gid, new_aliases in genre_new_aliases.items():
                await self._ensure_aliases(gid, new_aliases)

            if not raw_name_to_genres:
                continue

            # Build CTE with (raw_name, genre_id) pairs and INSERT mappings for this bucket's
            # tables. One raw name can produce multiple rows when it maps to multiple genres.
            cte_values = ", ".join(
                f"(LOWER('{name.replace(chr(39), chr(39) + chr(39))}'), {gid})"
                for name, gids in raw_name_to_genres.items()
                for gid in gids
            )
            cte = f"WITH genre_lookup(raw_name, genre_id) AS (VALUES {cte_values})"

            for table, media_type in tables:
                full_query = (
                    f"{cte} INSERT OR REPLACE INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}"
                    f"(genre_id, media_id, media_type, alias) "
                    f"SELECT gl.genre_id, {table}.item_id, "
                    f"'{media_type.value}', TRIM(g.value) "
                    f"FROM {table}, "
                    f"json_each(CASE WHEN json_valid({table}.metadata) "
                    f"THEN json_extract({table}.metadata, '$.genres') END) AS g "
                    f"JOIN genre_lookup gl ON gl.raw_name = LOWER(TRIM(g.value)) "
                    f"WHERE TRIM(g.value) != '' "
                    f"AND NOT EXISTS ("
                    f"SELECT 1 FROM {excl} e "
                    f"WHERE e.genre_id = gl.genre_id "
                    f"AND e.media_id = {table}.item_id "
                    f"AND e.media_type = '{media_type.value}')"
                )
                await db.execute(full_query)
            await db.commit()

        self.logger.info(
            "Bulk genre scan completed - mapped %d unique names to genres", total_resolved
        )
        await self._propagate_genre_mappings_to_parents()

    async def _cleanup_stale_genre_mappings(self) -> None:
        """
        Remove genre mappings where the alias is no longer in the media item's metadata.genres.

        A mapping is considered stale when the alias stored in the mapping is no longer present
        in the media item's current metadata.genres. This includes items where metadata.genres
        is empty or null — all mappings for such items are removed. Empty non-default genres
        (those without a translation_key) are also deleted.
        """
        db = self.mass.music.database
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING

        count_before = await db.get_count(gm)

        for table, media_type in MEDIA_TABLES:
            # Orphan pass: remove mappings whose media item no longer exists.
            # Runs regardless of is_manual — an orphan is always garbage.
            await db.delete_where_query(
                gm,
                f"media_type = '{media_type.value}' "
                f"AND NOT EXISTS ("
                f"  SELECT 1 FROM {table} "
                f"  WHERE {table}.item_id = {gm}.media_id"
                f")",
            )
            # Stale-alias pass: media item exists but the alias has dropped out
            # of metadata.genres. Manual mappings are excluded: their alias is
            # never written to metadata.genres.
            await db.delete_where_query(
                gm,
                f"media_type = '{media_type.value}' "
                f"AND alias IS NOT NULL "
                f"AND is_manual = 0 "
                f"AND NOT EXISTS ("
                f"  SELECT 1 FROM {table}, "
                f"  json_each(json_extract({table}.metadata, '$.genres')) AS g "
                f"  WHERE {table}.item_id = {gm}.media_id "
                f"  AND LOWER(TRIM(g.value)) = LOWER({gm}.alias)"
                f")",
            )
            # Cross-namespace pass: remove scanner-created mappings whose genre lives in a
            # different taxonomy than the item's media type. This re-homes legacy mappings
            # created before content_type namespacing (e.g. a podcast pointing at the music
            # "Spoken Word" genre); the scan then re-maps the item into its own taxonomy.
            # Manual mappings are preserved.
            expected = genre_content_type_for(media_type)
            expected_literal = "NULL" if expected is None else f"'{expected.value}'"
            await db.delete_where_query(
                gm,
                f"media_type = '{media_type.value}' "
                f"AND is_manual = 0 "
                f"AND genre_id IN ("
                f"  SELECT item_id FROM {DB_TABLE_GENRES} "
                f"  WHERE content_type IS NOT {expected_literal}"
                f")",
            )

        mappings_removed = count_before - await db.get_count(gm)
        if mappings_removed:
            self.logger.info("Genre scan: removed %d stale genre mappings", mappings_removed)

        # Delete playlog entries for empty non-default genres before removing them, to avoid
        # orphaned playlog rows pointing to genres that no longer exist.
        # is_default = 0 identifies non-default genres; default genres are always kept
        # even if they become unmapped/empty.
        excl = DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION
        await db.delete_where_query(
            DB_TABLE_PLAYLOG,
            f"media_type = '{MediaType.GENRE.value}' "
            f"AND item_id IN ("
            f"  SELECT item_id FROM {DB_TABLE_GENRES} "
            f"  WHERE is_default = 0 "
            f"  AND is_excluded = 0 "
            f"  AND NOT EXISTS ("
            f"    SELECT 1 FROM {gm} WHERE {gm}.genre_id = {DB_TABLE_GENRES}.item_id"
            f"  ) "
            f"  AND NOT EXISTS ("
            f"    SELECT 1 FROM {excl} WHERE {excl}.genre_id = {DB_TABLE_GENRES}.item_id"
            f"  )"
            f")",
        )
        genres_before = await db.get_count(DB_TABLE_GENRES)
        await db.delete_where_query(
            DB_TABLE_GENRES,
            f"is_default = 0 "
            f"AND is_excluded = 0 "
            f"AND NOT EXISTS ("
            f"  SELECT 1 FROM {gm} WHERE {gm}.genre_id = {DB_TABLE_GENRES}.item_id"
            f") "
            f"AND NOT EXISTS ("
            f"  SELECT 1 FROM {excl} WHERE {excl}.genre_id = {DB_TABLE_GENRES}.item_id"
            f")",
        )
        genres_deleted = genres_before - await db.get_count(DB_TABLE_GENRES)
        if genres_deleted:
            self.logger.info("Genre scan: deleted %d empty non-default genres", genres_deleted)

    async def _bulk_scan_unmapped_genres(self) -> int:
        """
        Scan only unmapped media items and create genre mappings using CTE.

        Similar to _bulk_scan_media_genres but filters to items not yet in
        genre_media_item_mapping. Used by the incremental scanner after syncs.

        :return: Total number of items mapped.
        """
        await self._cleanup_stale_genre_mappings()

        db = self.mass.music.database
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        excl = DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION
        count_before = await db.get_count(gm)
        mapped_any = False

        # Resolve and map each taxonomy (music / audiobook / podcast) separately so genre
        # names only resolve against — and new genres are created within — their own namespace.
        for content_type, tables in GENRE_BUCKETS:
            alias_to_genre, primary_name_to_genre = await self._build_genre_lookup(content_type)

            # Extract all unique raw genre names from this taxonomy's media items.
            # We don't filter by unmapped items here because a media item may have some
            # genres mapped but not all (e.g. added a new genre tag).
            union_parts = [
                f"SELECT DISTINCT TRIM(g.value) AS raw_name "
                f"FROM {table}, json_each(json_extract({table}.metadata, '$.genres')) AS g "
                f"WHERE json_extract({table}.metadata, '$.genres') IS NOT NULL "
                f"AND json_extract({table}.metadata, '$.genres') != '[]'"
                for table, _mtype in tables
            ]
            unique_names_sql = " UNION ".join(union_parts)
            rows = await db.get_rows_from_query(unique_names_sql, limit=0)
            unique_raw_names = [row["raw_name"] for row in rows if row["raw_name"]]
            if not unique_raw_names:
                continue

            # Resolve each raw name to genre_ids within this taxonomy. Primary-name match takes
            # priority over alias match so a bare "pop" tag only maps to the Pop genre, not every
            # genre that accumulated "pop" as a secondary alias.
            raw_name_to_genres: dict[str, list[int]] = {}
            for raw_name in unique_raw_names:
                norm = create_safe_string(raw_name.strip(), True, True)
                if not norm:
                    continue
                if norm in primary_name_to_genre:
                    raw_name_to_genres[raw_name] = [primary_name_to_genre[norm]]
                elif norm in alias_to_genre:
                    raw_name_to_genres[raw_name] = alias_to_genre[norm]
                else:
                    resolved_ids = await self._find_genres_for_alias(raw_name, content_type)
                    if resolved_ids:
                        raw_name_to_genres[raw_name] = resolved_ids
                        alias_to_genre[norm] = resolved_ids

            if not raw_name_to_genres:
                continue

            # Add discovered raw names as aliases to their resolved genres
            genre_new_aliases: dict[int, list[str]] = {}
            for raw_name, gids in raw_name_to_genres.items():
                for gid in gids:
                    genre_new_aliases.setdefault(gid, []).append(raw_name)
            for gid, new_aliases in genre_new_aliases.items():
                await self._ensure_aliases(gid, new_aliases)

            # Build CTE with n:n pairs and INSERT only for unmapped items
            cte_values = ", ".join(
                f"(LOWER('{name.replace(chr(39), chr(39) + chr(39))}'), {gid})"
                for name, gids in raw_name_to_genres.items()
                for gid in gids
            )
            cte = f"WITH genre_lookup(raw_name, genre_id) AS (VALUES {cte_values})"

            for table, media_type in tables:
                full_query = (
                    f"{cte} INSERT OR REPLACE INTO {gm}"
                    f"(genre_id, media_id, media_type, alias) "
                    f"SELECT gl.genre_id, {table}.item_id, "
                    f"'{media_type.value}', TRIM(g.value) "
                    f"FROM {table}, "
                    f"json_each(json_extract({table}.metadata, '$.genres')) AS g "
                    f"JOIN genre_lookup gl ON gl.raw_name = LOWER(TRIM(g.value)) "
                    f"WHERE json_extract({table}.metadata, '$.genres') IS NOT NULL "
                    f"AND json_extract({table}.metadata, '$.genres') != '[]' "
                    f"AND NOT EXISTS ("
                    f"SELECT 1 FROM {gm} ex "
                    f"WHERE ex.genre_id = gl.genre_id "
                    f"AND ex.media_id = {table}.item_id "
                    f"AND ex.media_type = '{media_type.value}' "
                    f"AND ex.is_derived = 0) "
                    f"AND NOT EXISTS ("
                    f"SELECT 1 FROM {excl} e "
                    f"WHERE e.genre_id = gl.genre_id "
                    f"AND e.media_id = {table}.item_id "
                    f"AND e.media_type = '{media_type.value}')"
                )
                await db.execute(full_query)
            mapped_any = True

        if mapped_any:
            await db.commit()
            await self._propagate_genre_mappings_to_parents()
        count_after = await db.get_count(gm)

        return count_after - count_before

    async def _propagate_genre_mappings_to_parents(self) -> None:
        """
        Propagate track genre mappings to albums and artists for filesystem provider instances.

        Only runs when at least one filesystem_local or filesystem_smb provider instance has
        the 'propagate_track_genres' config option enabled. Albums and artists that already
        have their own genre metadata (e.g. from an NFO file) are skipped.

        Derived mappings are stored with is_derived=1 and rebuilt from scratch on each
        call, so stale derived mappings are never left behind.
        The genre_media_item_exclusion table is respected — excluded pairs are never derived.
        """
        enabled_instance_ids: list[str] = []
        for p in self.mass.music.providers:
            if p.domain in {"filesystem_local", "filesystem_smb"}:
                enabled = await self.mass.config.get_provider_config_value(
                    p.instance_id, "propagate_track_genres", default=False
                )
                if enabled:
                    enabled_instance_ids.append(p.instance_id)

        db = self.mass.music.database
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING

        # Always wipe previously derived mappings first so that disabling propagation
        # on a provider immediately removes its derived entries, not just on next run.
        await db.execute(
            f"DELETE FROM {gm} WHERE is_derived = 1 AND media_type IN ('album', 'artist')"
        )

        if not enabled_instance_ids:
            await db.commit()
            return

        excl = DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION
        pm = DB_TABLE_PROVIDER_MAPPINGS
        ids_sql = ", ".join(f"'{x}'" for x in enabled_instance_ids)

        # Derive album genres: inherit each track genre mapping onto the track's album,
        # provided the album has no own genre metadata and the pair is not excluded.
        await db.execute(
            f"INSERT OR IGNORE INTO {gm} (genre_id, media_id, media_type, alias, is_derived) "
            f"SELECT DISTINCT m.genre_id, at.album_id, 'album', NULL, 1 "
            f"FROM {gm} m "
            f"JOIN {DB_TABLE_ALBUM_TRACKS} at "
            f"  ON m.media_id = at.track_id AND m.media_type = 'track' "
            f"JOIN {pm} p ON p.item_id = at.track_id AND p.media_type = 'track' "
            f"  AND p.provider_instance IN ({ids_sql}) "
            f"JOIN {DB_TABLE_ALBUMS} alb ON alb.item_id = at.album_id "
            f"WHERE ("
            f"  json_extract(alb.metadata, '$.genres') IS NULL "
            f"  OR json_extract(alb.metadata, '$.genres') = '[]'"
            f") "
            f"AND NOT EXISTS ("
            f"  SELECT 1 FROM {excl} e "
            f"  WHERE e.genre_id = m.genre_id "
            f"    AND e.media_id = at.album_id "
            f"    AND e.media_type = 'album'"
            f")"
        )

        # Derive artist genres: inherit each track genre mapping onto the track's artist,
        # provided the artist has no own genre metadata and the pair is not excluded.
        await db.execute(
            f"INSERT OR IGNORE INTO {gm} (genre_id, media_id, media_type, alias, is_derived) "
            f"SELECT DISTINCT m.genre_id, ta.artist_id, 'artist', NULL, 1 "
            f"FROM {gm} m "
            f"JOIN {DB_TABLE_TRACK_ARTISTS} ta "
            f"  ON m.media_id = ta.track_id AND m.media_type = 'track' "
            f"JOIN {pm} p ON p.item_id = ta.track_id AND p.media_type = 'track' "
            f"  AND p.provider_instance IN ({ids_sql}) "
            f"JOIN {DB_TABLE_ARTISTS} art ON art.item_id = ta.artist_id "
            f"WHERE ("
            f"  json_extract(art.metadata, '$.genres') IS NULL "
            f"  OR json_extract(art.metadata, '$.genres') = '[]'"
            f") "
            f"AND NOT EXISTS ("
            f"  SELECT 1 FROM {excl} e "
            f"  WHERE e.genre_id = m.genre_id "
            f"    AND e.media_id = ta.artist_id "
            f"    AND e.media_type = 'artist'"
            f")"
        )

        await db.commit()

    async def _find_genre_ids_for_alias(self, alias_norm: str) -> list[int]:
        """
        Return ids of non-excluded genres that claim the given alias.

        :param alias_norm: Alias normalised via ``create_safe_string``.
        """
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id, genre_aliases FROM {DB_TABLE_GENRES} WHERE is_excluded = 0",
            limit=0,
        )
        found: list[int] = []
        for row in rows:
            aliases = json.loads(row["genre_aliases"]) if row["genre_aliases"] else []
            if any(create_safe_string(a.strip(), True, True) == alias_norm for a in aliases):
                found.append(int(row["item_id"]))
        return found

    async def _seed_default_genres(
        self,
        content_type: MediaType | None,
        mapping: list[dict[str, Any]],
        full_restore: bool,
    ) -> list[int]:
        """
        Seed the curated default genres for a single taxonomy.

        Inserts missing default genres (is_default=1) scoped to ``content_type`` and tops up the
        aliases of any that already exist. Inserts are staged without committing.

        :param content_type: Taxonomy to seed (None = music/general).
        :param mapping: The curated genre/alias entries for this taxonomy.
        :param full_restore: When True the table was just wiped, so every entry is treated as new.
        :return: The item_ids of the genres created in this taxonomy.
        """
        content_type_value = content_type.value if content_type else None
        if full_restore:
            existing: set[str] = set()
        else:
            rows = await self.mass.music.database.get_rows_from_query(
                f"SELECT search_name FROM {DB_TABLE_GENRES} WHERE content_type IS :content_type",
                {"content_type": content_type_value},
                limit=0,
            )
            existing = {row["search_name"] for row in rows}

        created_ids: list[int] = []
        for entry in mapping:
            name = entry.get("genre")
            if not name:
                continue
            normalized = self._normalize_genre_name(name)
            if not normalized:
                continue
            name_value, sort_name, search_name, search_sort_name = normalized
            all_aliases = [name_value, *entry.get("aliases", [])]
            translation_key = entry.get("translation_key")
            icon_metadata = self._get_genre_icon_metadata(translation_key, content_type)

            # Partial restore: top up aliases on the existing genre and refresh its icon
            # (icons may have been added to the resources dir after it was first seeded).
            if search_name in existing:
                rows = await self.mass.music.database.get_rows_from_query(
                    f"SELECT item_id, metadata FROM {DB_TABLE_GENRES} "
                    "WHERE search_name = :search_name AND content_type IS :content_type",
                    {"search_name": search_name, "content_type": content_type_value},
                    limit=1,
                )
                if rows:
                    genre_id = int(rows[0]["item_id"])
                    await self._ensure_aliases(genre_id, all_aliases)
                    if icon_metadata is not None:
                        current_md = json.loads(rows[0]["metadata"]) if rows[0]["metadata"] else {}
                        fresh_images = icon_metadata.to_dict().get("images")
                        if current_md.get("images") != fresh_images:
                            current_md["images"] = fresh_images
                            await self.mass.music.database.update(
                                DB_TABLE_GENRES,
                                {"item_id": genre_id},
                                {"metadata": serialize_to_json(current_md)},
                            )
                continue

            # Stage new genre insert without committing yet (batch all in one transaction)
            cursor = await self.mass.music.database.execute(
                f"INSERT INTO {DB_TABLE_GENRES}"
                "(name, sort_name, translation_key, description, favorite, metadata, "
                "genre_aliases, play_count, last_played, "
                "search_name, search_sort_name, is_default, content_type) "
                "VALUES (:name, :sort_name, :translation_key, :description, :favorite, "
                ":metadata, :genre_aliases, :play_count, :last_played, "
                ":search_name, :search_sort_name, :is_default, :content_type)",
                {
                    "name": name_value,
                    "sort_name": sort_name,
                    "translation_key": translation_key,
                    "description": None,
                    "favorite": 0,
                    "metadata": serialize_to_json(icon_metadata.to_dict() if icon_metadata else {}),
                    "genre_aliases": serialize_to_json(all_aliases),
                    "play_count": 0,
                    "last_played": 0,
                    "search_name": search_name,
                    "search_sort_name": search_sort_name,
                    "is_default": 1,
                    "content_type": content_type_value,
                },
            )
            created_ids.append(cursor.lastrowid)
            existing.add(search_name)
        return created_ids

    async def _build_genre_lookup(
        self, content_type: MediaType | None
    ) -> tuple[dict[str, list[int]], dict[str, int]]:
        """
        Build alias and primary-name lookup dicts from the genres in a single taxonomy.

        :param content_type: Genre taxonomy to scope the lookup to (None = music/general).
        :return: Tuple of (alias_to_genre, primary_name_to_genre).
            alias_to_genre maps normalised alias -> list of genre_ids (n:n).
            primary_name_to_genre maps normalised primary name -> single genre_id.
        """
        alias_to_genre: dict[str, list[int]] = {}
        primary_name_to_genre: dict[str, int] = {}
        genre_rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id, search_name, genre_aliases FROM {DB_TABLE_GENRES} "
            "WHERE is_excluded = 0 AND content_type IS :content_type",
            {"content_type": content_type.value if content_type else None},
            limit=0,
        )
        for row in genre_rows:
            genre_id = int(row["item_id"])
            if row["search_name"]:
                primary_name_to_genre[row["search_name"]] = genre_id
            aliases = json.loads(row["genre_aliases"]) if row["genre_aliases"] else []
            for alias in aliases:
                norm = create_safe_string(alias.strip(), True, True)
                if norm:
                    alias_to_genre.setdefault(norm, [])
                    if genre_id not in alias_to_genre[norm]:
                        alias_to_genre[norm].append(genre_id)
        return alias_to_genre, primary_name_to_genre

    async def _resolve_genre_names_cached(
        self, genre_names: set[str], content_type: MediaType | None
    ) -> set[int] | None:
        """
        Resolve genre names to genre ids using a short-lived cached taxonomy snapshot.

        :param genre_names: Raw genre names from the provider.
        :param content_type: Genre taxonomy to resolve within (None = music/general).
        :return: The resolved genre ids, or None when any name is unknown to the
            taxonomy and a full resolution (with genre creation) is required.
        """
        cache_key = content_type.value if content_type else None
        lookup = self._sync_lookup_cache.get(cache_key)
        if lookup is None or (time.monotonic() - lookup.built_at) > SYNC_GENRE_LOOKUP_TTL:
            lookup = await self._build_sync_genre_lookup(content_type)
            self._sync_lookup_cache[cache_key] = lookup
        target_ids: set[int] = set()
        for name in genre_names:
            if not (normalized := self._normalize_genre_name(name)):
                continue
            search_name = normalized[2]
            # primary-name match takes priority over alias match, and names matching
            # an excluded genre deliberately resolve to nothing (mirrors
            # _find_genres_for_alias, which the full path uses)
            if (genre_id := lookup.primary_name_to_genre.get(search_name)) is not None:
                target_ids.add(genre_id)
            elif genre_ids := lookup.alias_to_genre.get(search_name):
                target_ids.update(genre_ids)
            elif search_name not in lookup.excluded_names:
                return None
        return target_ids

    async def _build_sync_genre_lookup(self, content_type: MediaType | None) -> _SyncGenreLookup:
        """Build a fresh in-memory genre lookup snapshot for a single taxonomy."""
        alias_to_genre, primary_name_to_genre = await self._build_genre_lookup(content_type)
        excluded_rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT search_name FROM {DB_TABLE_GENRES} "
            "WHERE is_excluded = 1 AND content_type IS :content_type",
            {"content_type": content_type.value if content_type else None},
            limit=0,
        )
        return _SyncGenreLookup(
            built_at=time.monotonic(),
            primary_name_to_genre=primary_name_to_genre,
            alias_to_genre=alias_to_genre,
            excluded_names={row["search_name"] for row in excluded_rows},
        )

    async def _ensure_aliases(self, genre_id: int, aliases: list[str]) -> None:
        """
        Ensure a genre has all the specified aliases in its genre_aliases JSON.

        :param genre_id: Database ID of the genre.
        :param aliases: List of alias strings that should be present.
        """
        genre = await self.get_library_item(genre_id)
        existing = list(genre.genre_aliases) if genre.genre_aliases else []
        merged = self._dedup_aliases(existing, aliases)
        if len(merged) != len(existing):
            await self.mass.music.database.update(
                self.db_table,
                {"item_id": genre_id},
                {"genre_aliases": serialize_to_json(merged)},
            )

    async def _find_genres_for_alias(self, name: str, content_type: MediaType | None) -> list[int]:
        """
        Find all genres in a taxonomy that own the given alias name, or create a new genre.

        An alias can map to multiple genres (n:n relationship). For example,
        "anime" could be an alias of both an "Anime" genre and an "Anime Music" genre.
        If no genre owns this alias, creates a new genre in this taxonomy.

        :param name: The alias name to find/create a genre for.
        :param content_type: Genre taxonomy to scope lookup/creation to (None = music/general).
        :return: List of genre IDs (empty if name is invalid).
        """
        normalized = self._normalize_genre_name(name)
        if not normalized:
            return []
        name_value, sort_name, search_name, search_sort_name = normalized
        content_type_value = content_type.value if content_type else None

        async with self._db_add_lock:
            found_ids: list[int] = []

            # Check if a non-excluded genre in this taxonomy exists with this name as its own
            # primary name. If so, return immediately — an exact primary-name match takes full
            # priority over alias scanning. This prevents broad tags like "pop" from fanning out
            # to every genre that accumulated "pop" as a secondary alias (Rock, Punk, etc.).
            primary = await self.mass.music.database.get_rows_from_query(
                f"SELECT item_id FROM {DB_TABLE_GENRES} "
                "WHERE search_name = :search_name AND is_excluded = 0 "
                "AND content_type IS :content_type",
                {"search_name": search_name, "content_type": content_type_value},
                limit=1,
            )
            if primary:
                return [int(primary[0]["item_id"])]

            # Search genre_aliases JSON columns (case-insensitive, can match multiple)
            rows = await self.mass.music.database.get_rows_from_query(
                f"SELECT item_id FROM {DB_TABLE_GENRES} "
                "WHERE is_excluded = 0 AND content_type IS :content_type AND EXISTS("
                "SELECT 1 FROM json_each(genre_aliases) "
                "WHERE LOWER(json_each.value) = LOWER(:alias_name)"
                ")",
                {"alias_name": name_value, "content_type": content_type_value},
                limit=0,
            )
            for row in rows:
                gid = int(row["item_id"])
                if gid not in found_ids:
                    found_ids.append(gid)

            # Also check via normalized comparison (create_safe_string).
            # This catches genres that stages 1-2 miss due to normalization
            # differences, e.g. genre A has "synthpop", genre B has "synth-pop"
            # — both normalize to "synthpop" but LOWER can't bridge the gap.
            all_genres = await self.mass.music.database.get_rows_from_query(
                f"SELECT item_id, genre_aliases FROM {DB_TABLE_GENRES} "
                "WHERE is_excluded = 0 AND content_type IS :content_type",
                {"content_type": content_type_value},
                limit=0,
            )
            for row in all_genres:
                aliases = json.loads(row["genre_aliases"]) if row["genre_aliases"] else []
                for alias in aliases:
                    if create_safe_string(alias.strip(), True, True) == search_name:
                        gid = int(row["item_id"])
                        if gid not in found_ids:
                            found_ids.append(gid)

            if found_ids:
                return found_ids

            # Check if this name was deliberately excluded in this taxonomy before creating
            excluded = await self.mass.music.database.get_rows_from_query(
                f"SELECT item_id FROM {DB_TABLE_GENRES} "
                "WHERE search_name = :search_name AND is_excluded = 1 "
                "AND content_type IS :content_type",
                {"search_name": search_name, "content_type": content_type_value},
                limit=1,
            )
            if excluded:
                return []

            # No genre owns this alias — create a new one in this taxonomy
            new_id = await self.mass.music.database.insert(
                DB_TABLE_GENRES,
                {
                    "name": name_value,
                    "sort_name": sort_name,
                    "description": None,
                    "favorite": 0,
                    "metadata": serialize_to_json({}),
                    "genre_aliases": serialize_to_json([name_value]),
                    "play_count": 0,
                    "last_played": 0,
                    "search_name": search_name,
                    "search_sort_name": search_sort_name,
                    "timestamp_added": UNSET,
                    "is_default": 0,
                    "content_type": content_type_value,
                },
            )
            return [new_id]

    async def _get_description(self, item_id: int) -> str | None:
        if db_row := await self.mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": item_id}):
            return dict(db_row).get("description")
        return None

    @staticmethod
    def _normalize_genre_name(raw_name: str) -> tuple[str, str, str, str] | None:
        """
        Normalize a raw genre name for storage and search.

        :param raw_name: Raw genre name from provider.
        :return: Tuple of (name, sort_name, search_name, search_sort_name) or None if invalid.
        """
        name = raw_name.strip()
        if not name:
            return None
        sort_name = name
        search_name = create_safe_string(name, True, True)
        if not search_name:
            return None
        search_sort_name = create_safe_string(sort_name or "", True, True)
        return name, sort_name, search_name, search_sort_name

    def _on_music_sync_completed(self, _event: MassEvent) -> None:
        """Trigger genre mapping scan when music sync tasks have completed."""
        self._queue_genre_mapping_scan_task()

    def _queue_genre_mapping_scan_task(self) -> BackgroundTask:
        """Queue the genre mapping scanner as a managed background task."""
        self.register_scheduled_scan_task()
        return self.mass.tasks.run_task(GENRE_SCAN_TASK_ID)

    def _get_genre_scan_task(self) -> BackgroundTask | None:
        """Return the latest managed genre scan task, if any."""
        try:
            return self.mass.tasks.get_task(GENRE_SCAN_TASK_ID)
        except InvalidDataError:
            return None

    @property
    def _genre_scan_running(self) -> bool:
        """Return whether the managed genre scan is currently queued or running."""
        if not (task := self._get_genre_scan_task()):
            return False
        return task.status in (TaskStatus.PENDING, TaskStatus.RUNNING)

    async def _scan_genre_mappings(self) -> None:
        """
        Scan media items with metadata.genres and map them to genres.

        Triggered after library sync completes or via manual API call.
        """
        # Double-check syncs haven't started since the event was dispatched
        if self.mass.music.active_sync_tasks:
            self.logger.debug("Syncs still in progress, deferring genre scan")
            update_current_task_progress_text("Waiting for music sync completion")
            return
        self._last_scan_time = time.time()

        try:
            self.logger.debug("Starting genre mapping scan...")
            update_current_task_progress_text("Scanning unmapped genre metadata")
            self._last_scan_mapped = await self._bulk_scan_unmapped_genres()
            update_current_task_progress_text(f"Mapped {self._last_scan_mapped} genre reference(s)")
            self.logger.info(
                "Genre mapping scan completed: %d items mapped (%.1fs)",
                self._last_scan_mapped,
                time.time() - self._last_scan_time,
            )

        except Exception as err:
            self.logger.error(
                "Error in genre mapping scanner: %s",
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )

    def _parse_summary_row(self, db_row: Mapping[str, Any]) -> GenreSummary:
        """Parse a raw summary db row into a GenreSummary object."""
        item = cast("GenreSummary", super()._parse_summary_row(db_row))
        # only overwrite the (name-derived) translation key when explicitly stored
        if translation_key := db_row["translation_key"]:
            item.translation_key = translation_key
        if content_type := db_row["content_type"]:
            item.content_type = MediaType(content_type)
        return item
