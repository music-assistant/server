"""Manage MediaItems of type Genre and GenreAlias."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Genre,
    GenreAlias,
    RecommendationFolder,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING,
    DB_TABLE_ALIASES,
    DB_TABLE_GENRE_ALIAS_MAPPING,
    DB_TABLE_GENRES,
    DEFAULT_GENRE_MAPPING,
)
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import json_loads, serialize_to_json

from .base import MediaControllerBase

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class GenreController(MediaControllerBase[Genre]):
    """Controller for Genre and GenreAlias entities."""

    db_table = DB_TABLE_GENRES
    media_type = MediaType.GENRE
    item_cls = Genre

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        self._db_add_lock = asyncio.Lock()
        self.base_query = f"SELECT {DB_TABLE_GENRES}.* FROM {DB_TABLE_GENRES}"
        self.alias_base_query = f"""
        SELECT
            {DB_TABLE_ALIASES}.*
        FROM {DB_TABLE_ALIASES}
        """

        # register (extra) api handlers for alias CRUD and mappings
        self.mass.register_api_command("music/aliases/library_items", self.alias_library_items)
        self.mass.register_api_command("music/aliases/get", self.get_alias)
        self.mass.register_api_command(
            "music/aliases/add", self.add_alias_to_library, required_role="admin"
        )
        self.mass.register_api_command(
            "music/aliases/update", self.update_alias_in_library, required_role="admin"
        )
        self.mass.register_api_command(
            "music/aliases/remove", self.remove_alias_from_library, required_role="admin"
        )
        self.mass.register_api_command(
            "music/genres/add_alias_mapping", self.add_alias_mapping, required_role="admin"
        )
        self.mass.register_api_command(
            "music/genres/remove_alias_mapping", self.remove_alias_mapping, required_role="admin"
        )
        self.mass.register_api_command(
            "music/aliases/add_media_mapping", self.add_media_mapping, required_role="admin"
        )
        self.mass.register_api_command(
            "music/aliases/remove_media_mapping",
            self.remove_media_mapping,
            required_role="admin",
        )
        self.mass.register_api_command(
            "music/aliases/promote_to_genre",
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
            self.add_genre_to_library,
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

    async def _add_library_item(self, item: Genre, overwrite_existing: bool = False) -> int:
        """Add a new genre record to the database."""
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
                "play_count": 0,
                "last_played": 0,
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": UNSET,
            },
        )
        await self._ensure_self_alias(db_id, item.name)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def add_genre_to_library(self, item: Genre, overwrite_existing: bool = False) -> Genre:
        """Add genre to library and ensure its alias mapping."""
        genre = await self.add_item_to_library(item, overwrite_existing)
        await self._ensure_self_alias(int(genre.item_id), genre.name)
        return genre

    async def _update_library_item(
        self, item_id: str | int, update: Genre, overwrite: bool = False
    ) -> None:
        """Update existing genre record in the database."""
        db_id = int(item_id)  # ensure integer
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
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": UNSET,
            },
        )
        await self._ensure_self_alias(db_id, name)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def search(
        self,
        query: str,
        provider_instance_id_or_domain: str | None = None,
        limit: int = 25,
    ) -> list[Genre]:
        """Search for genres in the library."""
        if provider_instance_id_or_domain and provider_instance_id_or_domain != "library":
            return []
        items = await self.library_items(search=query, limit=limit)
        await self._attach_aliases(items)
        return items

    async def library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre_ids: int | list[int] | None = None,
    ) -> list[Genre]:
        """Get genres in the library and attach aliases.

        :param favorite: Filter by favorite status.
        :param search: Search query string.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: Sort order (name, name_desc, sort_name, sort_name_desc).
        :param provider: Filter by provider (not used for genres).
        :param genre_ids: NOT SUPPORTED - Filtering genres by genres doesn't make sense.
        """
        if genre_ids is not None:
            msg = "genre_ids parameter is not supported for Genre.library_items()"
            raise ValueError(msg)

        query_parts: list[str] = []
        query_params: dict[str, Any] = {}
        if search:
            search_value = create_safe_string(search, True, True)
            query_parts.append(f"{DB_TABLE_GENRES}.search_name LIKE :search")
            query_params["search"] = f"%{search_value}%"
        if favorite is not None:
            query_parts.append(f"{DB_TABLE_GENRES}.favorite = :favorite")
            query_params["favorite"] = favorite
        sql_query = self.base_query
        if query_parts:
            sql_query += " WHERE " + " AND ".join(query_parts)
        sql_query += f" GROUP BY {DB_TABLE_GENRES}.item_id"
        if order_by in ("name", "name_desc"):
            sql_query += (
                f" ORDER BY {DB_TABLE_GENRES}.search_name "
                f"{'DESC' if order_by.endswith('desc') else 'ASC'}"
            )
        else:
            sql_query += (
                f" ORDER BY {DB_TABLE_GENRES}.search_sort_name "
                f"{'DESC' if order_by.endswith('desc') else 'ASC'}"
            )
        rows = await self.mass.music.database.get_rows_from_query(
            sql_query, query_params, limit=limit, offset=offset
        )
        items = [Genre.from_dict(self._parse_genre_row(row)) for row in rows]
        await self._attach_aliases(items)
        return items

    async def get_library_item(self, item_id: int | str) -> Genre:
        """Get single genre and attach aliases."""
        db_id = int(item_id)
        if not (
            db_row := await self.mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": db_id})
        ):
            msg = f"genre not found in library: {db_id}"
            raise MediaNotFoundError(msg)
        item = Genre.from_dict(self._parse_genre_row(db_row))
        await self._attach_aliases([item])
        return item

    async def get(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> Genre:
        """Return (full) details for a single genre (library only)."""
        if provider_instance_id_or_domain != "library":
            msg = f"genre not found in provider: {provider_instance_id_or_domain}"
            raise MediaNotFoundError(msg)
        return await self.get_library_item(item_id)

    async def radio_mode_base_tracks(
        self,
        item: Genre,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Get the list of base tracks for a genre.

        :param item: The Genre to get base tracks for.
        :param preferred_provider_instances: List of preferred provider instance IDs to use.
        """
        db_id = int(item.item_id)
        subquery = f"SELECT alias_id FROM {DB_TABLE_GENRE_ALIAS_MAPPING} WHERE genre_id = :genre_id"
        query = (
            f"tracks.item_id in (SELECT media_id FROM {DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING} "
            f"WHERE media_type = 'track' AND alias_id in ({subquery}))"
        )
        return await self.mass.music.tracks.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={"genre_id": db_id},
        )

    async def mapped_media(
        self,
        item: Genre,
        limit: int = 0,
        offset: int = 0,
    ) -> tuple[list[Track], list[Album], list[Artist]]:
        """Return tracks, albums, and artists mapped to aliases for a genre."""
        db_id = int(item.item_id)
        subquery = f"SELECT alias_id FROM {DB_TABLE_GENRE_ALIAS_MAPPING} WHERE genre_id = :genre_id"

        track_query = (
            f"tracks.item_id in (SELECT media_id FROM {DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING} "
            f"WHERE media_type = 'track' AND alias_id in ({subquery}))"
        )
        album_query = (
            f"albums.item_id in (SELECT media_id FROM {DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING} "
            f"WHERE media_type = 'album' AND alias_id in ({subquery}))"
        )
        artist_query = (
            f"artists.item_id in (SELECT media_id FROM {DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING} "
            f"WHERE media_type = 'artist' AND alias_id in ({subquery}))"
        )

        tracks = await self.mass.music.tracks.get_library_items_by_query(
            extra_query_parts=[track_query],
            extra_query_params={"genre_id": db_id},
            limit=limit,
            offset=offset,
        )
        albums = await self.mass.music.albums.get_library_items_by_query(
            extra_query_parts=[album_query],
            extra_query_params={"genre_id": db_id},
            limit=limit,
            offset=offset,
        )
        artists = await self.mass.music.artists.get_library_items_by_query(
            extra_query_parts=[artist_query],
            extra_query_params={"genre_id": db_id},
            limit=limit,
            offset=offset,
        )
        return tracks, albums, artists

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
        subquery = f"SELECT alias_id FROM {DB_TABLE_GENRE_ALIAS_MAPPING} WHERE genre_id = :genre_id"
        media_rows: list[tuple[MediaType, str]] = [
            (MediaType.ARTIST, "Artists"),
            (MediaType.ALBUM, "Albums"),
            (MediaType.TRACK, "Tracks"),
            (MediaType.PLAYLIST, "Playlists"),
            (MediaType.RADIO, "Radio"),
            (MediaType.PODCAST, "Podcasts"),
            (MediaType.AUDIOBOOK, "Audiobooks"),
        ]
        rows: list[RecommendationFolder] = []
        for media_type, title in media_rows:
            ctrl = self.mass.music.get_controller(media_type)
            query = (
                f"{ctrl.db_table}.item_id in (SELECT media_id "
                f"FROM {DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING} "
                f"WHERE media_type = :media_type AND alias_id in ({subquery}))"
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
                continue
            rows.append(
                RecommendationFolder(
                    item_id=f"genre_{media_type.value}",
                    name=title,
                    provider="library",
                    items=UniqueList(items[:limit]),
                )
            )
        return rows

    async def match_providers(self, db_item: Genre) -> None:
        """No provider matching for genres at this time."""
        return

    async def restore_default_genres(self, full_restore: bool = False) -> list[Genre]:
        """Restore default genres from genre_mapping.json.

        :param full_restore: If True, delete all existing genres and recreate from defaults.
                            If False (default), only add missing genres and ensure aliases exist.
        """
        # Full restore: Delete all existing genres and start fresh
        if full_restore:
            self.logger.warning("Performing FULL restore - deleting all existing genres")
            # Delete all genre mappings first (due to foreign key constraints)
            await self.mass.music.database.delete(DB_TABLE_GENRE_ALIAS_MAPPING)
            # Delete all alias-media mappings
            await self.mass.music.database.delete(DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING)
            # Delete all aliases
            await self.mass.music.database.delete(DB_TABLE_ALIASES)
            # Delete all genres
            await self.mass.music.database.delete(DB_TABLE_GENRES)
            existing = set()
        else:
            # Partial restore: Get existing genres to avoid duplicates
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

            # Partial restore: Ensure self-alias and configured aliases exist
            if search_name in existing:
                if db_row := await self.mass.music.database.get_row(
                    DB_TABLE_GENRES, {"search_name": search_name}
                ):
                    genre_id = int(db_row["item_id"])
                    await self._ensure_self_alias(genre_id, name_value)
                    for alias_name in entry.get("aliases", []):
                        await self._ensure_alias_for_genre(genre_id, alias_name)
                continue

            # Create new genre
            genre_id = await self.mass.music.database.insert(
                DB_TABLE_GENRES,
                {
                    "name": name_value,
                    "sort_name": sort_name,
                    "translation_key": entry.get("translation_key"),
                    "description": None,
                    "favorite": 0,
                    "metadata": serialize_to_json({}),
                    "external_ids": serialize_to_json(set()),
                    "play_count": 0,
                    "last_played": 0,
                    "search_name": search_name,
                    "search_sort_name": search_sort_name,
                },
            )
            await self._ensure_self_alias(genre_id, name_value)
            for alias_name in entry.get("aliases", []):
                await self._ensure_alias_for_genre(genre_id, alias_name)
            created_ids.append(genre_id)
            existing.add(search_name)

        if not created_ids:
            return []
        return [await self.get_library_item(item_id) for item_id in created_ids]

    async def remove_item_from_library(self, item_id: str | int, recursive: bool = True) -> None:
        """Delete genre record from the database."""
        db_id = int(item_id)
        item = await self.get_library_item(db_id)
        await self.mass.music.database.delete(DB_TABLE_GENRE_ALIAS_MAPPING, {"genre_id": db_id})
        await self.mass.music.database.delete(DB_TABLE_GENRES, {"item_id": db_id})
        self.mass.signal_event(EventType.MEDIA_ITEM_DELETED, item.uri, item)
        self.logger.debug("deleted genre with id %s from database", db_id)

    async def add_alias_to_library(
        self, item: GenreAlias, overwrite_existing: bool = False
    ) -> GenreAlias:
        """Add alias to library and return the new (or updated) item."""
        existing_id = await self._get_alias_id_by_name(item.name)
        if existing_id:
            await self._update_alias_item(existing_id, item, overwrite_existing)
            alias_item = await self.get_alias(existing_id)
            self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, alias_item.uri, alias_item)
            return alias_item
        db_id = await self._add_alias_item(item)
        alias_item = await self.get_alias(db_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_ADDED, alias_item.uri, alias_item)
        return alias_item

    async def update_alias_in_library(
        self, item_id: str | int, update: GenreAlias, overwrite: bool = False
    ) -> GenreAlias:
        """Update existing alias record in the database."""
        db_id = int(item_id)
        await self._update_alias_item(db_id, update, overwrite)
        alias_item = await self.get_alias(db_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, alias_item.uri, alias_item)
        return alias_item

    async def remove_alias_from_library(self, item_id: str | int) -> None:
        """Delete alias record from the database.

        :param item_id: Database ID of the alias to delete.
        :raises ValueError: If the alias is a self-alias (cannot be deleted).
        """
        db_id = int(item_id)
        with_item = await self.get_alias(db_id)

        # Prevent deletion of self-aliases (aliases that match their parent genre name)
        if hasattr(with_item, "genres") and with_item.genres:
            for genre in with_item.genres:
                if genre.name.lower() == with_item.name.lower():
                    msg = (
                        f"Cannot delete self-alias '{with_item.name}'. "
                        f"Self-aliases are automatically created with genres and "
                        f"cannot be deleted. "
                        f"If you want to remove this genre, delete the genre "
                        f"'{genre.name}' instead."
                    )
                    raise ValueError(msg)

        await self.mass.music.database.delete(DB_TABLE_GENRE_ALIAS_MAPPING, {"alias_id": db_id})
        await self.mass.music.database.delete(
            DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING, {"alias_id": db_id}
        )
        await self.mass.music.database.delete(DB_TABLE_ALIASES, {"item_id": db_id})
        self.mass.signal_event(EventType.MEDIA_ITEM_DELETED, with_item.uri, with_item)
        self.logger.debug("deleted alias with id %s from database", db_id)

    async def get_alias(self, item_id: str | int) -> GenreAlias:
        """Get single alias by id with its parent genres attached."""
        db_id = int(item_id)
        sql_query = f"{self.alias_base_query} WHERE {DB_TABLE_ALIASES}.item_id = :item_id"
        rows = await self.mass.music.database.get_rows_from_query(
            sql_query, {"item_id": db_id}, limit=1
        )
        if not rows:
            msg = f"alias not found in library: {db_id}"
            raise MediaNotFoundError(msg)
        alias = GenreAlias.from_dict(self._parse_alias_row(rows[0]))
        await self._attach_genres([alias])
        return alias

    async def alias_library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
    ) -> list[GenreAlias]:
        """Get aliases in the library with their parent genres attached."""
        query_parts: list[str] = []
        query_params: dict[str, Any] = {}
        if search:
            search_value = create_safe_string(search, True, True)
            query_parts.append(f"{DB_TABLE_ALIASES}.search_name LIKE :search")
            query_params["search"] = f"%{search_value}%"
        if favorite is not None:
            query_parts.append(f"{DB_TABLE_ALIASES}.favorite = :favorite")
            query_params["favorite"] = favorite
        sql_query = self.alias_base_query
        if query_parts:
            sql_query += " WHERE " + " AND ".join(query_parts)
        sql_query += f" GROUP BY {DB_TABLE_ALIASES}.item_id"
        if order_by in ("name", "name_desc"):
            sql_query += (
                f" ORDER BY {DB_TABLE_ALIASES}.search_name "
                f"{'DESC' if order_by.endswith('desc') else 'ASC'}"
            )
        else:
            sql_query += (
                f" ORDER BY {DB_TABLE_ALIASES}.search_sort_name "
                f"{'DESC' if order_by.endswith('desc') else 'ASC'}"
            )
        rows = await self.mass.music.database.get_rows_from_query(
            sql_query, query_params, limit=limit, offset=offset
        )
        aliases = [GenreAlias.from_dict(self._parse_alias_row(row)) for row in rows]
        await self._attach_genres(aliases)
        return aliases

    async def add_alias_mapping(self, genre_id: str | int, alias_id: str | int) -> None:
        """Map alias to a genre.

        :param genre_id: Database ID of the genre.
        :param alias_id: Database ID of the alias to map to this genre.
        """
        await self.mass.music.database.insert(
            DB_TABLE_GENRE_ALIAS_MAPPING,
            {"genre_id": int(genre_id), "alias_id": int(alias_id)},
            allow_replace=True,
        )
        updated_genre = await self.get_library_item(int(genre_id))
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, updated_genre.uri, updated_genre)

    async def remove_alias_mapping(self, genre_id: str | int, alias_id: str | int) -> None:
        """Remove alias mapping from a genre.

        :param genre_id: Database ID of the genre.
        :param alias_id: Database ID of the alias to remove from this genre.
        :raises ValueError: If trying to unlink a genre from its own self-alias.
        """
        db_genre_id = int(genre_id)
        db_alias_id = int(alias_id)

        # Prevent unlinking a genre from its own self-alias
        genre = await self.get_library_item(db_genre_id)
        alias = await self.get_alias(db_alias_id)

        if genre.name.lower() == alias.name.lower():
            msg = (
                f"Cannot unlink self-alias '{alias.name}' from genre '{genre.name}'. "
                f"Self-aliases are automatically created with genres and cannot be unlinked. "
                f"If you want to remove this genre, delete the genre instead."
            )
            raise ValueError(msg)

        await self.mass.music.database.delete(
            DB_TABLE_GENRE_ALIAS_MAPPING,
            {"genre_id": db_genre_id, "alias_id": db_alias_id},
        )
        updated_genre = await self.get_library_item(db_genre_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, updated_genre.uri, updated_genre)

    async def add_media_mapping(
        self, alias_id: str | int, media_type: MediaType, media_id: str | int
    ) -> None:
        """Map alias to a media item.

        Supports mapping aliases to tracks, albums, artists, or genres.

        :param alias_id: Database ID of the alias.
        :param media_type: Type of media item (track, album, artist, genre).
        :param media_id: Database ID of the media item.
        """
        if media_type == MediaType.GENRE:
            await self.add_alias_mapping(media_id, alias_id)
            return
        await self.mass.music.database.insert(
            DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING,
            {
                "media_type": media_type.value,
                "media_id": int(media_id),
                "alias_id": int(alias_id),
            },
            allow_replace=True,
        )

    async def remove_media_mapping(
        self, alias_id: str | int, media_type: MediaType, media_id: str | int
    ) -> None:
        """Remove alias mapping from a media item.

        Supports removing aliases from tracks, albums, artists, or genres.

        :param alias_id: Database ID of the alias.
        :param media_type: Type of media item (track, album, artist, genre).
        :param media_id: Database ID of the media item.
        """
        if media_type == MediaType.GENRE:
            await self.remove_alias_mapping(media_id, alias_id)
            return
        await self.mass.music.database.delete(
            DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING,
            {
                "media_type": media_type.value,
                "media_id": int(media_id),
                "alias_id": int(alias_id),
            },
        )

    async def promote_alias_to_genre(self, alias_id: str | int) -> Genre:
        """Promote an alias to become a standalone parent genre.

        Creates a new Genre with the alias's name and metadata, then remaps the alias
        to belong to the new genre instead of its current parent genre(s). This is useful
        when an alias should become its own top-level genre category.

        :param alias_id: The database ID of the alias to promote.
        :return: The newly created Genre object.
        """
        db_alias_id = int(alias_id)

        # Get the alias to be promoted
        alias = await self.get_alias(db_alias_id)

        # Prevent promoting self-aliases (aliases that match their parent genre name)
        if hasattr(alias, "genres") and alias.genres:
            for genre in alias.genres:
                if genre.name.lower() == alias.name.lower():
                    msg = (
                        f"Cannot promote self-alias '{alias.name}'. "
                        f"This alias is the primary alias for the genre '{genre.name}' "
                        "and promoting it would create a duplicate genre."
                    )
                    raise ValueError(msg)

        # Create a new Genre with the alias's name and metadata
        new_genre = Genre(
            item_id="0",  # Will be assigned by database
            provider="library",
            name=alias.name,
            sort_name=alias.sort_name,
            translation_key=None,
            metadata=alias.metadata,
            external_ids=alias.external_ids,
            provider_mappings=set(),
            favorite=False,
        )

        # Add the genre to the library (this also creates the self-alias)
        created_genre = await self.add_genre_to_library(new_genre)

        # Remove all existing genre→alias mappings for this alias
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_ALIAS_MAPPING, {"alias_id": db_alias_id}
        )

        # Map the alias to the newly created genre
        await self.add_alias_mapping(created_genre.item_id, db_alias_id)

        # Return the newly created genre with all aliases attached
        return await self.get_library_item(int(created_genre.item_id))

    async def sync_media_item_genres(
        self, media_type: MediaType, media_id: str | int, genre_names: set[str]
    ) -> None:
        """Sync genre mappings for a media item.

        Ensures genre and alias records exist and updates alias-media mappings.
        Removes mappings that are no longer present in the incoming genre_names set.

        :param media_type: The type of media item being synced.
        :param media_id: The database ID of the media item.
        :param genre_names: Set of genre names from the provider (empty set removes all mappings).
        """
        media_id_int = int(media_id)
        normalized_names = []
        for name in genre_names:
            normalized = self._normalize_genre_name(name)
            if not normalized:
                continue
            normalized_names.append(normalized[0])

        # Build target set of alias IDs from incoming genre names
        alias_ids: set[int] = set()
        for name in normalized_names:
            result = await self._ensure_genre_and_alias(name)
            if result:
                alias_ids.add(result[1])

        # Get current alias IDs from database
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT alias_id FROM {DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING} "
            "WHERE media_type = :media_type AND media_id = :media_id",
            {"media_type": media_type.value, "media_id": media_id_int},
            limit=0,
        )
        existing_alias_ids = {int(row["alias_id"]) for row in rows}

        # Calculate additions and removals
        to_add = alias_ids - existing_alias_ids
        to_remove = existing_alias_ids - alias_ids

        # Remove outdated mappings
        for alias_id in to_remove:
            await self.mass.music.database.delete(
                DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING,
                {
                    "media_type": media_type.value,
                    "media_id": media_id_int,
                    "alias_id": alias_id,
                },
            )

        # Add new mappings
        for alias_id in to_add:
            await self.mass.music.database.insert(
                DB_TABLE_ALIAS_MEDIA_ITEM_MAPPING,
                {
                    "media_type": media_type.value,
                    "media_id": media_id_int,
                    "alias_id": alias_id,
                },
                allow_replace=True,
            )

    async def _attach_aliases(self, genres: list[Genre]) -> None:
        """Attach genre aliases to genre objects by querying mapping tables.

        Populates the genre_aliases field for each genre with the set of aliases
        mapped to that genre in the database.

        :param genres: List of Genre objects to populate with their aliases.
        """
        if not genres:
            return
        genre_ids = [int(g.item_id) for g in genres]
        sql_query = f"""
            SELECT
                m.genre_id AS genre_id,
                a.*
            FROM {DB_TABLE_GENRE_ALIAS_MAPPING} m
            JOIN {DB_TABLE_ALIASES} a ON a.item_id = m.alias_id
            WHERE m.genre_id IN :genre_ids
        """
        rows = await self.mass.music.database.get_rows_from_query(
            sql_query, {"genre_ids": genre_ids}, limit=0
        )
        by_genre: dict[int, set[GenreAlias]] = defaultdict(set)
        for row in rows:
            row_dict = dict(row)
            genre_id = int(row_dict.pop("genre_id"))
            alias_item = GenreAlias.from_dict(self._parse_alias_row(row_dict))
            by_genre[genre_id].add(alias_item)
        for genre in genres:
            aliases = by_genre.get(int(genre.item_id), set())
            if hasattr(genre, "genre_aliases"):
                genre.genre_aliases = aliases

    async def _attach_genres(self, aliases: list[GenreAlias]) -> None:
        """Attach parent genres to alias objects by querying mapping tables.

        Populates the genres field for each alias with the set of genres
        that this alias is mapped to in the database.

        :param aliases: List of GenreAlias objects to populate with their parent genres.
        """
        if not aliases:
            return
        alias_ids = [int(a.item_id) for a in aliases]
        sql_query = f"""
            SELECT
                m.alias_id AS alias_id,
                g.*
            FROM {DB_TABLE_GENRE_ALIAS_MAPPING} m
            JOIN {DB_TABLE_GENRES} g ON g.item_id = m.genre_id
            WHERE m.alias_id IN :alias_ids
        """
        rows = await self.mass.music.database.get_rows_from_query(
            sql_query, {"alias_ids": alias_ids}, limit=0
        )
        by_alias: dict[int, set[Genre]] = defaultdict(set)
        for row in rows:
            row_dict = dict(row)
            alias_id = int(row_dict.pop("alias_id"))
            genre_item = Genre.from_dict(self._parse_genre_row(row_dict))
            by_alias[alias_id].add(genre_item)
        for alias in aliases:
            genres = by_alias.get(int(alias.item_id), set())
            if hasattr(alias, "genres"):
                alias.genres = genres

    async def _add_alias_item(self, item: GenreAlias) -> int:
        return await self.mass.music.database.insert(
            DB_TABLE_ALIASES,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
                "play_count": 0,
                "last_played": 0,
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": UNSET,
            },
        )

    async def _update_alias_item(
        self, item_id: int, update: GenreAlias, overwrite: bool = False
    ) -> None:
        cur_item = await self.get_alias(item_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        await self.mass.music.database.update(
            DB_TABLE_ALIASES,
            {"item_id": item_id},
            {
                "name": name,
                "sort_name": sort_name,
                "favorite": update.favorite,
                "metadata": serialize_to_json(metadata),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
            },
        )

    async def _get_alias_id_by_name(self, name: str) -> int | None:
        search_name = create_safe_string(name, True, True)
        if not search_name:
            return None
        if db_row := await self.mass.music.database.get_row(
            DB_TABLE_ALIASES, {"search_name": search_name}
        ):
            return int(db_row["item_id"])
        return None

    async def _get_genre_id_by_name(self, name: str) -> int | None:
        search_name = create_safe_string(name, True, True)
        if not search_name:
            return None
        if db_row := await self.mass.music.database.get_row(
            DB_TABLE_GENRES, {"search_name": search_name}
        ):
            return int(db_row["item_id"])
        return None

    async def _get_genre_id_by_alias(self, alias_id: int) -> int | None:
        if db_row := await self.mass.music.database.get_row(
            DB_TABLE_GENRE_ALIAS_MAPPING, {"alias_id": alias_id}
        ):
            return int(db_row["genre_id"])
        return None

    async def _ensure_genre_and_alias(self, name: str) -> tuple[int, int] | None:
        """Ensure both genre and alias records exist for a given name.

        Creates genre and alias records if they don't exist, and establishes the
        mapping between them. Thread-safe via database lock.

        :param name: Raw genre name from provider.
        :return: Tuple of (genre_id, alias_id) if successful, None if name invalid.
        """
        normalized = self._normalize_genre_name(name)
        if not normalized:
            return None
        name_value, sort_name, search_name, search_sort_name = normalized

        async with self._db_add_lock:
            genre_id = await self._get_genre_id_by_name(name_value)
            alias_id = await self._get_alias_id_by_name(name_value)

            if not genre_id and alias_id:
                genre_id = await self._get_genre_id_by_alias(alias_id)

            if not genre_id:
                genre_id = await self.mass.music.database.insert(
                    DB_TABLE_GENRES,
                    {
                        "name": name_value,
                        "sort_name": sort_name,
                        "description": None,
                        "favorite": 0,
                        "metadata": serialize_to_json({}),
                        "external_ids": serialize_to_json(set()),
                        "play_count": 0,
                        "last_played": 0,
                        "search_name": search_name,
                        "search_sort_name": search_sort_name,
                        "timestamp_added": UNSET,
                    },
                )

            if not alias_id:
                alias_id = await self.mass.music.database.insert(
                    DB_TABLE_ALIASES,
                    {
                        "name": name_value,
                        "sort_name": sort_name,
                        "favorite": 0,
                        "metadata": serialize_to_json({}),
                        "external_ids": serialize_to_json(set()),
                        "play_count": 0,
                        "last_played": 0,
                        "search_name": search_name,
                        "search_sort_name": search_sort_name,
                        "timestamp_added": UNSET,
                    },
                )

            await self.mass.music.database.insert(
                DB_TABLE_GENRE_ALIAS_MAPPING,
                {"genre_id": genre_id, "alias_id": alias_id},
                allow_replace=True,
            )

        return genre_id, alias_id

    async def _ensure_self_alias(self, genre_id: int, name: str) -> None:
        """Ensure a self-alias exists for a genre with the genre's own name.

        Creates an alias matching the genre's name and maps it to the genre.
        Used when adding default genres to ensure they have at least one alias.

        :param genre_id: Database ID of the genre.
        :param name: Name to use for the alias (typically the genre's name).
        """
        normalized = self._normalize_genre_name(name)
        if not normalized:
            return
        alias_id = await self._get_alias_id_by_name(name)
        if not alias_id:
            name_value, sort_name, search_name, search_sort_name = normalized
            alias_id = await self.mass.music.database.insert(
                DB_TABLE_ALIASES,
                {
                    "name": name_value,
                    "sort_name": sort_name,
                    "favorite": 0,
                    "metadata": serialize_to_json({}),
                    "external_ids": serialize_to_json(set()),
                    "play_count": 0,
                    "last_played": 0,
                    "search_name": search_name,
                    "search_sort_name": search_sort_name,
                },
            )
        await self.mass.music.database.insert(
            DB_TABLE_GENRE_ALIAS_MAPPING,
            {"genre_id": genre_id, "alias_id": alias_id},
            allow_replace=True,
        )

    async def _ensure_alias_for_genre(self, genre_id: int, name: str) -> None:
        """Ensure an alias exists for a genre and map them together.

        Creates the alias if it doesn't exist and establishes the mapping to the
        specified genre. Used when seeding default alias mappings.

        :param genre_id: Database ID of the genre to map to.
        :param name: Name of the alias to create/map.
        """
        normalized = self._normalize_genre_name(name)
        if not normalized:
            return
        name_value, sort_name, search_name, search_sort_name = normalized
        alias_id = await self._get_alias_id_by_name(name_value)
        if not alias_id:
            alias_id = await self.mass.music.database.insert(
                DB_TABLE_ALIASES,
                {
                    "name": name_value,
                    "sort_name": sort_name,
                    "favorite": 0,
                    "metadata": serialize_to_json({}),
                    "external_ids": serialize_to_json(set()),
                    "play_count": 0,
                    "last_played": 0,
                    "search_name": search_name,
                    "search_sort_name": search_sort_name,
                },
            )
        await self.mass.music.database.insert(
            DB_TABLE_GENRE_ALIAS_MAPPING,
            {"genre_id": genre_id, "alias_id": alias_id},
            allow_replace=True,
        )

    async def _get_description(self, item_id: int) -> str | None:
        if db_row := await self.mass.music.database.get_row(DB_TABLE_GENRES, {"item_id": item_id}):
            return dict(db_row).get("description")
        return None

    def _parse_genre_row(self, db_row: Any) -> dict[str, Any]:
        """Parse a database row from genres table into Genre model dict.

        Converts database representation to the format expected by Genre.from_dict(),
        including JSON deserialization and type conversions.

        :param db_row: Raw database row from genres table.
        :return: Dictionary suitable for Genre.from_dict().
        """
        row = dict(db_row)
        metadata = json_loads(row.get("metadata") or "{}")
        external_ids = json_loads(row.get("external_ids") or "[]")
        return {
            "item_id": str(row["item_id"]),
            "provider": "library",
            "name": row["name"],
            "sort_name": row.get("sort_name") or row["name"],
            "version": row.get("version") or "",
            "translation_key": row.get("translation_key"),
            "provider_mappings": set(),
            "external_ids": set(external_ids),
            "metadata": metadata,
            "favorite": bool(row.get("favorite")),
            "genre_aliases": set(),  # Will be populated by _attach_aliases() if needed
        }

    def _parse_alias_row(self, db_row: Any) -> dict[str, Any]:
        """Parse a database row from aliases table into GenreAlias model dict.

        Converts database representation to the format expected by GenreAlias.from_dict(),
        including JSON deserialization and type conversions.

        :param db_row: Raw database row from aliases table.
        :return: Dictionary suitable for GenreAlias.from_dict().
        """
        row = dict(db_row)
        metadata = json_loads(row.get("metadata") or "{}")
        external_ids = json_loads(row.get("external_ids") or "[]")
        return {
            "item_id": str(row["item_id"]),
            "provider": "library",
            "name": row["name"],
            "sort_name": row.get("sort_name") or row["name"],
            "version": row.get("version") or "",
            "provider_mappings": set(),
            "external_ids": set(external_ids),
            "metadata": metadata,
            "favorite": bool(row.get("favorite")),
            "genres": None,  # Will be populated by _attach_genres() if needed
        }

    @staticmethod
    def _normalize_genre_name(raw_name: str) -> tuple[str, str, str, str] | None:
        """Normalize a raw genre name for storage and search.

        Creates display name, sort name, and search-safe variants for database storage.

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
