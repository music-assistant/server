"""Manage MediaItems of type Genre."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import EventType, ImageType, MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    MediaItemImage,
    MediaItemMetadata,
    RecommendationFolder,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PODCASTS,
    DB_TABLE_RADIOS,
    DB_TABLE_TRACKS,
    DEFAULT_GENRE_MAPPING,
    GENRE_ICONS_DIR,
)
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import serialize_to_json

from .base import MediaControllerBase

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant import MusicAssistant


class GenreController(MediaControllerBase[Genre]):
    """Controller for Genre entities."""

    db_table = DB_TABLE_GENRES
    media_type = MediaType.GENRE
    item_cls = Genre

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        # Background scanner state tracking
        self._scanner_running: bool = False
        self._last_scan_time: float = 0
        self._last_scan_mapped: int = 0
        self.base_query = f"""
        SELECT
            {DB_TABLE_GENRES}.*,
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
        FROM {DB_TABLE_GENRES}"""

        # register extra api handlers
        self.mass.register_api_command(
            "music/genres/add_alias", self.add_alias, required_role="admin"
        )
        self.mass.register_api_command(
            "music/genres/remove_alias", self.remove_alias, required_role="admin"
        )
        self.mass.register_api_command(
            "music/genres/add_media_mapping", self.add_media_mapping, required_role="admin"
        )
        self.mass.register_api_command(
            "music/genres/remove_media_mapping",
            self.remove_media_mapping,
            required_role="admin",
        )
        self.mass.register_api_command(
            "music/genres/promote_alias",
            self.promote_alias_to_genre,
            required_role="admin",
        )
        self.mass.register_api_command(
            "music/genres/restore_defaults",
            self.restore_default_genres,
            required_role="admin",
        )
        self.mass.register_api_command(
            "music/genres/add",
            self.add_item_to_library,
            required_role="admin",
        )
        self.mass.register_api_command(
            "music/genres/overview",
            self.get_overview,
        )
        self.mass.register_api_command(
            "music/genres/radio_mode_base_tracks",
            self.get_radio_mode_base_tracks,
        )
        self.mass.register_api_command(
            "music/genres/scan_mappings",
            self.scan_mappings,
            required_role="admin",
        )
        self.mass.register_api_command(
            "music/genres/scanner_status",
            self.get_scanner_status,
        )
        self.mass.register_api_command(
            "music/genres/genres_for_media_item",
            self.get_genres_for_media_item,
        )

        # Run genre mapping scanner after library sync completes
        self.mass.subscribe(self._on_sync_tasks_updated, EventType.SYNC_TASKS_UPDATED)

    @staticmethod
    def _get_genre_icon_metadata(translation_key: str | None) -> MediaItemMetadata | None:
        """Build metadata with genre icon image if an SVG exists for the translation key.

        :param translation_key: The genre's translation key (matches SVG filename).
        """
        if not translation_key:
            return None
        icon_path = GENRE_ICONS_DIR / f"{translation_key}.svg"
        if not icon_path.is_file():
            return None
        image = MediaItemImage(
            type=ImageType.THUMB,
            path=str(icon_path),
            provider="builtin",
        )
        return MediaItemMetadata(images=UniqueList([image]))

    @staticmethod
    def _dedup_aliases(existing: list[str], new: list[str]) -> list[str]:
        """Merge alias lists, deduplicating by normalized form (create_safe_string).

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

    @property
    def _search_filter_clause(self) -> str:
        """Return search filter that also matches genre aliases."""
        return (
            f"({self.db_table}.search_name LIKE :search"
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
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "translation_key": item.translation_key,
                "description": item.metadata.description if item.metadata else None,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
                "genre_aliases": serialize_to_json(aliases),
                "play_count": 0,
                "last_played": 0,
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": UNSET,
            },
        )
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
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "genre_aliases": serialize_to_json(merged_aliases),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": UNSET,
            },
        )
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        **kwargs: Any,
    ) -> list[Genre]:
        """Get genres in the library.

        :param genre: NOT SUPPORTED - Filtering genres by genres doesn't make sense.
        """
        if genre is not None:
            msg = "genre parameter is not supported for Genre.library_items()"
            raise ValueError(msg)
        # Genres are library-only items without provider_mappings, so ignore
        # the provider filter (the frontend always sends provider="library").
        # Pass raw lowered search for alias matching (search_raw),
        # since the normalized :search param strips spaces/special chars.
        extra_params: dict[str, Any] | None = None
        if search:
            extra_params = {"search_raw": f"%{search.strip().lower()}%"}
        return await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            extra_query_params=extra_params,
        )

    async def radio_mode_base_tracks(
        self,
        item: Genre,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """Get the list of base tracks for a genre.

        :param item: The Genre to get base tracks for.
        :param preferred_provider_instances: List of preferred provider instance IDs to use.
        """
        db_id = int(item.item_id)
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING
        query = (
            f"EXISTS(SELECT 1 FROM {gm} gm "
            "WHERE gm.media_id = tracks.item_id "
            "AND gm.media_type = 'track' "
            "AND gm.genre_id = :genre_id)"
        )
        return await self.mass.music.tracks.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={"genre_id": db_id},
            limit=50,
            order_by="random",
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
        """Return tracks, albums, and artists mapped to a genre.

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

        track_query = (
            f"EXISTS(SELECT 1 FROM {gm} gm "
            "WHERE gm.media_id = tracks.item_id "
            "AND gm.media_type = 'track' AND gm.genre_id = :genre_id)"
        )
        album_query = (
            f"EXISTS(SELECT 1 FROM {gm} gm "
            "WHERE gm.media_id = albums.item_id "
            "AND gm.media_type = 'album' AND gm.genre_id = :genre_id)"
        )
        artist_query = (
            f"EXISTS(SELECT 1 FROM {gm} gm "
            "WHERE gm.media_id = artists.item_id "
            "AND gm.media_type = 'artist' AND gm.genre_id = :genre_id)"
        )

        tracks, albums, artists = await asyncio.gather(
            self.mass.music.tracks.get_library_items_by_query(
                extra_query_parts=[track_query],
                extra_query_params={"genre_id": db_id},
                limit=t_limit,
                offset=offset,
                order_by=order_by,
            ),
            self.mass.music.albums.get_library_items_by_query(
                extra_query_parts=[album_query],
                extra_query_params={"genre_id": db_id},
                limit=a_limit,
                offset=offset,
                order_by=order_by,
            ),
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
        """Return all genres mapped to a given media item.

        :param media_type: The type of media item.
        :param media_id: The database ID of the media item.
        """
        media_id_int = int(media_id)
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

    async def get_radio_mode_base_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str | None = None,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """Return base tracks for genre radio mode."""
        provider = provider_instance_id_or_domain or "library"
        item = await self.get(item_id, provider)
        return await self.radio_mode_base_tracks(item, preferred_provider_instances)

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
        media_rows: list[tuple[MediaType, str]] = [
            (MediaType.ARTIST, "Artists"),
            (MediaType.ALBUM, "Albums"),
            (MediaType.TRACK, "Tracks"),
            (MediaType.PLAYLIST, "Playlists"),
            (MediaType.RADIO, "Radio"),
            (MediaType.PODCAST, "Podcasts"),
            (MediaType.AUDIOBOOK, "Audiobooks"),
        ]

        async def _fetch_media_type(
            media_type: MediaType, title: str
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
                provider="library",
                items=UniqueList(items[:limit]),
            )

        results = await asyncio.gather(*[_fetch_media_type(mt, title) for mt, title in media_rows])
        return [r for r in results if r is not None]

    async def match_providers(self, db_item: Genre) -> None:
        """No provider matching for genres at this time."""
        return

    async def restore_default_genres(self, full_restore: bool = False) -> list[Genre]:
        """Restore default genres from genre_mapping.json.

        :param full_restore: If True, delete all existing genres and recreate from defaults.
                            If False (default), only add missing genres and ensure aliases exist.
        """
        if full_restore:
            self.logger.warning("Performing FULL restore - deleting all existing genres")
            await self.mass.music.database.delete(DB_TABLE_GENRE_MEDIA_ITEM_MAPPING)
            await self.mass.music.database.delete(DB_TABLE_GENRES)
            existing = set()
        else:
            rows = await self.mass.music.database.get_rows_from_query(
                f"SELECT search_name FROM {DB_TABLE_GENRES}", limit=0
            )
            existing = {row["search_name"] for row in rows}

        created_ids: list[int] = []
        for entry in DEFAULT_GENRE_MAPPING:
            name = entry.get("genre")
            if not name:
                continue
            normalized = self._normalize_genre_name(name)
            if not normalized:
                continue
            name_value, sort_name, search_name, search_sort_name = normalized
            all_aliases = [name_value, *entry.get("aliases", [])]

            # Partial restore: Ensure aliases are up to date
            if search_name in existing:
                if db_row := await self.mass.music.database.get_row(
                    DB_TABLE_GENRES, {"search_name": search_name}
                ):
                    genre_id = int(db_row["item_id"])
                    await self._ensure_aliases(genre_id, all_aliases)
                continue

            # Create new genre
            translation_key = entry.get("translation_key")
            icon_metadata = self._get_genre_icon_metadata(translation_key)
            genre_id = await self.mass.music.database.insert(
                DB_TABLE_GENRES,
                {
                    "name": name_value,
                    "sort_name": sort_name,
                    "translation_key": translation_key,
                    "description": None,
                    "favorite": 0,
                    "metadata": serialize_to_json(icon_metadata.to_dict() if icon_metadata else {}),
                    "external_ids": serialize_to_json(set()),
                    "genre_aliases": serialize_to_json(all_aliases),
                    "play_count": 0,
                    "last_played": 0,
                    "search_name": search_name,
                    "search_sort_name": search_sort_name,
                    "timestamp_added": UNSET,
                },
            )
            created_ids.append(genre_id)
            existing.add(search_name)

        if full_restore:
            await self._bulk_scan_media_genres()

        if not created_ids:
            return []
        return [await self.get_library_item(item_id) for item_id in created_ids]

    async def _bulk_scan_media_genres(self) -> None:
        """Bulk-scan all media items and rebuild genre mappings using CTE.

        Uses the same approach as the initial migration: extracts all unique genre names
        from metadata.genres across all media tables, resolves them to genre IDs via alias
        lookup, then does a single INSERT per media type using a CTE join.
        """
        db = self.mass.music.database

        media_tables = (
            (DB_TABLE_TRACKS, MediaType.TRACK),
            (DB_TABLE_ALBUMS, MediaType.ALBUM),
            (DB_TABLE_ARTISTS, MediaType.ARTIST),
            (DB_TABLE_PLAYLISTS, MediaType.PLAYLIST),
            (DB_TABLE_RADIOS, MediaType.RADIO),
            (DB_TABLE_AUDIOBOOKS, MediaType.AUDIOBOOK),
            (DB_TABLE_PODCASTS, MediaType.PODCAST),
        )

        # Build alias -> genre_ids lookup from all genres in the database.
        # One alias can map to multiple genres (n:n relationship).
        alias_to_genre: dict[str, list[int]] = {}
        genre_rows = await db.get_rows_from_query(
            f"SELECT item_id, genre_aliases FROM {DB_TABLE_GENRES}", limit=0
        )
        for row in genre_rows:
            genre_id = int(row["item_id"])
            aliases = json.loads(row["genre_aliases"]) if row["genre_aliases"] else []
            for alias in aliases:
                norm = create_safe_string(alias.strip(), True, True)
                if norm:
                    alias_to_genre.setdefault(norm, [])
                    if genre_id not in alias_to_genre[norm]:
                        alias_to_genre[norm].append(genre_id)

        # Extract all unique raw genre names from metadata across all media tables
        union_parts = [
            f"SELECT DISTINCT TRIM(g.value) AS raw_name "
            f"FROM {table}, json_each(json_extract({table}.metadata, '$.genres')) AS g "
            f"WHERE json_extract({table}.metadata, '$.genres') IS NOT NULL "
            f"AND json_extract({table}.metadata, '$.genres') != '[]'"
            for table, _ in media_tables
        ]
        unique_names_sql = " UNION ".join(union_parts)
        rows = await db.get_rows_from_query(unique_names_sql, limit=0)
        unique_raw_names = [row["raw_name"] for row in rows if row["raw_name"]]

        self.logger.debug(
            "Bulk genre scan - discovered %d unique genre names", len(unique_raw_names)
        )

        # Resolve each raw name to genre_ids via alias lookup.
        # One raw name can map to multiple genres (n:n).
        raw_name_to_genres: dict[str, list[int]] = {}
        for raw_name in unique_raw_names:
            norm = create_safe_string(raw_name.strip(), True, True)
            if not norm:
                continue
            if norm in alias_to_genre:
                raw_name_to_genres[raw_name] = alias_to_genre[norm]
                self.logger.debug(
                    "Bulk scan - resolved %r -> genre_ids %s (alias match)",
                    raw_name,
                    alias_to_genre[norm],
                )
            else:
                resolved_ids = await self._find_genres_for_alias(raw_name)
                if resolved_ids:
                    raw_name_to_genres[raw_name] = resolved_ids
                    alias_to_genre[norm] = resolved_ids
                    self.logger.debug(
                        "Bulk scan - resolved %r -> genre_ids %s (new genre)",
                        raw_name,
                        resolved_ids,
                    )

        self.logger.info(
            "Bulk genre scan - resolved %d unique genre names", len(raw_name_to_genres)
        )

        # Add discovered raw names as aliases to their resolved genres so that
        # future searches by raw name (e.g. "Synthpop") find the parent genre
        # even when the stored alias differs (e.g. "synth-pop").
        genre_new_aliases: dict[int, list[str]] = {}
        for raw_name, gids in raw_name_to_genres.items():
            for gid in gids:
                genre_new_aliases.setdefault(gid, []).append(raw_name)
        for gid, new_aliases in genre_new_aliases.items():
            await self._ensure_aliases(gid, new_aliases)

        # Build CTE with (raw_name, genre_id) pairs. One raw name can produce
        # multiple rows when it maps to multiple genres (n:n).
        if raw_name_to_genres:
            cte_values = ", ".join(
                f"(LOWER('{name.replace(chr(39), chr(39) + chr(39))}'), {gid})"
                for name, gids in raw_name_to_genres.items()
                for gid in gids
            )
            cte = f"WITH genre_lookup(raw_name, genre_id) AS (VALUES {cte_values})"

            for table, media_type in media_tables:
                full_query = (
                    f"{cte} INSERT OR REPLACE INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}"
                    f"(genre_id, media_id, media_type, alias) "
                    f"SELECT gl.genre_id, {table}.item_id, "
                    f"'{media_type.value}', TRIM(g.value) "
                    f"FROM {table}, "
                    f"json_each(json_extract({table}.metadata, '$.genres')) AS g "
                    f"JOIN genre_lookup gl ON gl.raw_name = LOWER(TRIM(g.value)) "
                    f"WHERE json_extract({table}.metadata, '$.genres') IS NOT NULL "
                    f"AND json_extract({table}.metadata, '$.genres') != '[]'"
                )
                await db.execute(full_query)
            await db.commit()

        self.logger.info(
            "Bulk genre scan completed - mapped %d unique names to genres",
            len(raw_name_to_genres),
        )

    async def _bulk_scan_unmapped_genres(self) -> int:
        """Scan only unmapped media items and create genre mappings using CTE.

        Similar to _bulk_scan_media_genres but filters to items not yet in
        genre_media_item_mapping. Used by the incremental scanner after syncs.

        :return: Total number of items mapped.
        """
        db = self.mass.music.database
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING

        media_tables = (
            (DB_TABLE_TRACKS, MediaType.TRACK),
            (DB_TABLE_ALBUMS, MediaType.ALBUM),
            (DB_TABLE_ARTISTS, MediaType.ARTIST),
            (DB_TABLE_PLAYLISTS, MediaType.PLAYLIST),
            (DB_TABLE_RADIOS, MediaType.RADIO),
            (DB_TABLE_AUDIOBOOKS, MediaType.AUDIOBOOK),
            (DB_TABLE_PODCASTS, MediaType.PODCAST),
        )

        # Build alias -> genre_ids lookup (n:n) from all genres in the database.
        alias_to_genre: dict[str, list[int]] = {}
        genre_rows = await db.get_rows_from_query(
            f"SELECT item_id, genre_aliases FROM {DB_TABLE_GENRES}", limit=0
        )
        for row in genre_rows:
            genre_id = int(row["item_id"])
            aliases = json.loads(row["genre_aliases"]) if row["genre_aliases"] else []
            for alias in aliases:
                norm = create_safe_string(alias.strip(), True, True)
                if norm:
                    alias_to_genre.setdefault(norm, [])
                    if genre_id not in alias_to_genre[norm]:
                        alias_to_genre[norm].append(genre_id)

        # Extract all unique raw genre names from media items.
        # We don't filter by unmapped items here because a media item may
        # have some genres mapped but not all (e.g. added a new genre tag).
        union_parts = [
            f"SELECT DISTINCT TRIM(g.value) AS raw_name "
            f"FROM {table}, json_each(json_extract({table}.metadata, '$.genres')) AS g "
            f"WHERE json_extract({table}.metadata, '$.genres') IS NOT NULL "
            f"AND json_extract({table}.metadata, '$.genres') != '[]'"
            for table, _mtype in media_tables
        ]
        unique_names_sql = " UNION ".join(union_parts)
        rows = await db.get_rows_from_query(unique_names_sql, limit=0)
        unique_raw_names = [row["raw_name"] for row in rows if row["raw_name"]]

        if not unique_raw_names:
            return 0

        self.logger.debug(
            "Incremental genre scan - discovered %d unique genre names from unmapped items",
            len(unique_raw_names),
        )

        # Resolve each raw name to genre_ids (n:n)
        raw_name_to_genres: dict[str, list[int]] = {}
        for raw_name in unique_raw_names:
            norm = create_safe_string(raw_name.strip(), True, True)
            if not norm:
                continue
            if norm in alias_to_genre:
                raw_name_to_genres[raw_name] = alias_to_genre[norm]
                self.logger.debug(
                    "Scanner - resolved %r -> genre_ids %s (alias match)",
                    raw_name,
                    alias_to_genre[norm],
                )
            else:
                resolved_ids = await self._find_genres_for_alias(raw_name)
                if resolved_ids:
                    raw_name_to_genres[raw_name] = resolved_ids
                    alias_to_genre[norm] = resolved_ids
                    self.logger.debug(
                        "Scanner - resolved %r -> genre_ids %s (new genre)",
                        raw_name,
                        resolved_ids,
                    )

        if not raw_name_to_genres:
            return 0

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

        count_before = await db.get_count(gm)
        for table, media_type in media_tables:
            full_query = (
                f"{cte} INSERT OR IGNORE INTO {gm}"
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
                f"AND ex.media_type = '{media_type.value}')"
            )
            await db.execute(full_query)
        await db.commit()
        count_after = await db.get_count(gm)

        return count_after - count_before

    async def remove_item_from_library(self, item_id: str | int, recursive: bool = True) -> None:
        """Delete genre record from the database."""
        db_id = int(item_id)
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING, {"genre_id": db_id}
        )
        await super().remove_item_from_library(item_id, recursive)

    async def add_alias(self, genre_id: str | int, alias: str) -> Genre:
        """Add an alias string to a genre.

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
        """Remove an alias string from a genre.

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
        updated = await self.get_library_item(db_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, updated.uri, updated)
        return updated

    async def add_media_mapping(
        self, genre_id: str | int, media_type: MediaType, media_id: str | int, alias: str
    ) -> None:
        """Map a media item to a genre.

        :param genre_id: Database ID of the genre.
        :param media_type: Type of media item (track, album, artist).
        :param media_id: Database ID of the media item.
        :param alias: The alias string that caused this mapping.
        """
        await self.mass.music.database.insert(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": int(genre_id),
                "media_id": int(media_id),
                "media_type": media_type.value,
                "alias": alias,
            },
            allow_replace=True,
        )

    async def remove_media_mapping(
        self, genre_id: str | int, media_type: MediaType, media_id: str | int
    ) -> None:
        """Remove a media item mapping from a genre.

        :param genre_id: Database ID of the genre.
        :param media_type: Type of media item (track, album, artist).
        :param media_id: Database ID of the media item.
        """
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {
                "genre_id": int(genre_id),
                "media_id": int(media_id),
                "media_type": media_type.value,
            },
        )

    async def promote_alias_to_genre(self, genre_id: str | int, alias: str) -> Genre:
        """Promote an alias to become a standalone genre.

        Creates a new Genre with the alias's name, moves all media mappings
        for that alias to the new genre, and removes the alias from the
        original genre.

        :param genre_id: Database ID of the source genre.
        :param alias: The alias string to promote.
        :return: The newly created Genre.
        """
        db_genre_id = int(genre_id)
        source_genre = await self.get_library_item(db_genre_id)

        if create_safe_string(alias, True, True) == create_safe_string(
            source_genre.name, True, True
        ):
            msg = (
                f"Cannot promote self-alias '{alias}'. "
                f"This alias is the primary name for genre '{source_genre.name}'."
            )
            raise ValueError(msg)

        # Create new genre with the alias as its name
        new_genre = Genre(
            item_id="0",
            provider="library",
            name=alias,
            sort_name=alias,
            translation_key=None,
            provider_mappings=set(),
            favorite=False,
        )
        created_genre = await self.add_item_to_library(new_genre)
        new_genre_id = int(created_genre.item_id)

        # Move media mappings from source genre to new genre for this alias (case-insensitive)
        await self.mass.music.database.execute(
            f"UPDATE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
            "SET genre_id = :new_id WHERE genre_id = :old_id AND LOWER(alias) = LOWER(:alias)",
            {"new_id": new_genre_id, "old_id": db_genre_id, "alias": alias},
        )

        # Remove alias from source genre (normalized comparison)
        alias_norm = create_safe_string(alias, True, True)
        aliases = list(source_genre.genre_aliases) if source_genre.genre_aliases else []
        aliases = [a for a in aliases if create_safe_string(a, True, True) != alias_norm]
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_genre_id},
            {"genre_aliases": serialize_to_json(list(aliases))},
        )

        return await self.get_library_item(new_genre_id)

    async def sync_media_item_genres(
        self, media_type: MediaType, media_id: str | int, genre_names: set[str]
    ) -> None:
        """Sync genre mappings for a media item.

        Ensures genre records exist and updates genre-media mappings.
        Removes mappings that are no longer present in the incoming genre_names set.

        :param media_type: The type of media item being synced.
        :param media_id: The database ID of the media item.
        :param genre_names: Set of genre names from the provider.
        """
        media_id_int = int(media_id)
        gm = DB_TABLE_GENRE_MEDIA_ITEM_MAPPING

        # Build target set: (genre_id, alias_name) from incoming names.
        # One alias can map to multiple genres (n:n).
        target_mappings: dict[int, str] = {}
        for name in genre_names:
            normalized = self._normalize_genre_name(name)
            if not normalized:
                continue
            genre_ids = await self._find_genres_for_alias(normalized[0])
            for gid in genre_ids:
                if gid not in target_mappings:
                    target_mappings[gid] = normalized[0]

        # Get current genre_ids from database
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT genre_id FROM {gm} WHERE media_type = :media_type AND media_id = :media_id",
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

    async def _ensure_aliases(self, genre_id: int, aliases: list[str]) -> None:
        """Ensure a genre has all the specified aliases in its genre_aliases JSON.

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

    async def _find_genres_for_alias(self, name: str) -> list[int]:
        """Find all genres that own the given alias name, or create a new genre.

        An alias can map to multiple genres (n:n relationship). For example,
        "anime" could be an alias of both an "Anime" genre and an "Anime Music" genre.
        If no genre owns this alias, creates a new genre.

        :param name: The alias name to find/create a genre for.
        :return: List of genre IDs (empty if name is invalid).
        """
        normalized = self._normalize_genre_name(name)
        if not normalized:
            return []
        name_value, sort_name, search_name, search_sort_name = normalized

        async with self._db_add_lock:
            found_ids: list[int] = []

            # Check if a genre exists with this name as its own name
            if db_row := await self.mass.music.database.get_row(
                DB_TABLE_GENRES, {"search_name": search_name}
            ):
                found_ids.append(int(db_row["item_id"]))

            # Search genre_aliases JSON columns (case-insensitive, can match multiple)
            rows = await self.mass.music.database.get_rows_from_query(
                f"SELECT item_id FROM {DB_TABLE_GENRES} "
                "WHERE EXISTS("
                "SELECT 1 FROM json_each(genre_aliases) "
                "WHERE LOWER(json_each.value) = LOWER(:alias_name)"
                ")",
                {"alias_name": name_value},
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
                f"SELECT item_id, genre_aliases FROM {DB_TABLE_GENRES}", limit=0
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

            # No genre owns this alias — create a new one
            new_id = await self.mass.music.database.insert(
                DB_TABLE_GENRES,
                {
                    "name": name_value,
                    "sort_name": sort_name,
                    "description": None,
                    "favorite": 0,
                    "metadata": serialize_to_json({}),
                    "external_ids": serialize_to_json(set()),
                    "genre_aliases": serialize_to_json([name_value]),
                    "play_count": 0,
                    "last_played": 0,
                    "search_name": search_name,
                    "search_sort_name": search_sort_name,
                    "timestamp_added": UNSET,
                },
            )
            return [new_id]

    async def _get_description(self, item_id: int) -> str | None:
        if db_row := await self.mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": item_id}):
            return dict(db_row).get("description")
        return None

    @staticmethod
    def _normalize_genre_name(raw_name: str) -> tuple[str, str, str, str] | None:
        """Normalize a raw genre name for storage and search.

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

    def _on_sync_tasks_updated(self, _event: MassEvent) -> None:
        """Trigger genre mapping scan when all sync tasks complete."""
        if self.mass.music.in_progress_syncs or self._scanner_running:
            return
        self._scanner_running = True
        self.mass.create_task(self._scan_genre_mappings())

    async def _scan_genre_mappings(self) -> None:
        """Scan media items with metadata.genres and map them to genres.

        Triggered after library sync completes or via manual API call.
        Callers must set _scanner_running = True before calling this method.
        """
        # Double-check syncs haven't started since the event was dispatched
        if self.mass.music.in_progress_syncs:
            self.logger.debug("Syncs still in progress, deferring genre scan")
            self._scanner_running = False
            return
        self._last_scan_time = time.time()

        try:
            self.logger.debug("Starting genre mapping scan...")
            self._last_scan_mapped = await self._bulk_scan_unmapped_genres()
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

        finally:
            self._scanner_running = False

    async def scan_mappings(self) -> dict[str, Any]:
        """Manually trigger a genre mapping scan (admin only).

        :return: Status information about the scan trigger.
        """
        if self._scanner_running:
            return {
                "status": "already_running",
                "message": "Genre mapping scanner is already running",
            }

        self._scanner_running = True
        self.mass.create_task(self._scan_genre_mappings())

        return {
            "status": "triggered",
            "message": "Genre mapping scan triggered",
            "last_scan": self._last_scan_time,
        }

    async def get_scanner_status(self) -> dict[str, Any]:
        """Get status of the genre mapping background scanner.

        :return: Scanner status information.
        """
        return {
            "running": self._scanner_running,
            "last_scan_time": self._last_scan_time,
            "last_scan_ago_seconds": (
                int(time.time() - self._last_scan_time) if self._last_scan_time else None
            ),
            "last_scan_mapped": self._last_scan_mapped,
        }
