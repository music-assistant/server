"""GenresController: Controller holding all logic for Genres."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    Genre,
    Playlist,
    Podcast,
    Track,
)

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent

    from music_assistant import MusicAssistant

from music_assistant.constants import (
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_GENRES,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PODCASTS,
    DB_TABLE_TRACKS,
)
from music_assistant.controllers.media.base import MediaControllerBase
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.json import json_dumps, json_loads, serialize_to_json

# Define your Base Genres constant
BASE_GENRES = [
    "Alternative",
    "Ambient",
    "Blues",
    "Classical",
    "Comedy",
    "Country",
    "Dance",
    "Disco",
    "Drum & Bass",
    "Dubstep",
    "Electronic",
    "Folk",
    "Funk",
    "Hip Hop",
    "Holiday",
    "House",
    "Indie",
    "Instrumental",
    "Jazz",
    "K-Pop",
    "Latin",
    "Metal",
    "New Age",
    "Opera",
    "Pop",
    "Punk",
    "R&B",
    "Rap",
    "Reggae",
    "Rock",
    "Ska",
    "Soul",
    "Soundtrack",
    "Spoken Word",
    "Techno",
    "Trance",
    "Trap",
    "World",
]


class GenresController(MediaControllerBase[Genre]):
    """Controller holding all logic for Genres."""

    db_table = DB_TABLE_GENRES
    media_type = MediaType.GENRE
    item_cls = Genre

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        self.mass.register_api_command("music/genres/genre_tracks", self.genre_tracks)
        self.mass.register_api_command("music/genres/genre_albums", self.genre_albums)
        self.mass.register_api_command("music/genres/genre_artists", self.genre_artists)
        self.mass.register_api_command("music/genres/genre_playlists", self.genre_playlists)
        self.mass.register_api_command("music/genres/genre_podcasts", self.genre_podcasts)
        self.mass.register_api_command("music/genres/genre_audiobooks", self.genre_audiobooks)
        self.mass.register_api_command(
            "music/genres/add_alias", self.add_alias, required_role="admin"
        )
        self.mass.register_api_command(
            "music/genres/remove_alias", self.remove_alias, required_role="admin"
        )
        self.mass.register_api_command("music/genres/create", self.create, required_role="admin")
        self.mass.register_api_command(
            "music/genres/merge", self.merge_genres, required_role="admin"
        )
        self.mass.register_api_command(
            "music/genres/split", self.split_genre, required_role="admin"
        )
        # register event listeners
        self.mass.subscribe(self._on_item_added, EventType.MEDIA_ITEM_ADDED)
        self.mass.subscribe(self._on_item_added, EventType.MEDIA_ITEM_UPDATED)
        # base query is just the table, aliases are in genre_mappings JSON
        self.base_query = f"SELECT {self.db_table}.* FROM {self.db_table} "

    def _apply_filters(
        self,
        query_parts: list[str],
        query_params: dict[str, Any],
        join_parts: list[str],
        favorite: bool | None,
        search: str | None,
        provider_filter: list[str] | None,
        genre_filter: list[int] | None = None,
    ) -> None:
        """Apply search, favorite, and provider filters.

        :param genre_filter: Optional list of genre library item IDs to filter by.
        """
        # NOTE: We do NOT call super()._apply_filters() here because
        # we do not want to apply provider filters to genres (they are global).

        # 1. Handle Search (Custom for Genres)
        if search:
            # custom search for genres to include aliases from genre_mappings
            search_query = (
                f"({self.db_table}.search_name LIKE :search OR "
                f"json_extract({self.db_table}.genre_mappings, '$.aliases') "
                f"LIKE '%' || :search || '%')"
            )
            if genre_filter:
                ids_str = ",".join(str(x) for x in genre_filter)
                query_parts.append(f"({search_query} OR {self.db_table}.item_id IN ({ids_str}))")
            else:
                query_parts.append(search_query)
        elif genre_filter:
            ids_str = ",".join(str(x) for x in genre_filter)
            query_parts.append(f"{self.db_table}.item_id IN ({ids_str})")

        # 2. Handle Favorite
        if favorite is not None:
            query_parts.append(f"{self.db_table}.favorite = :favorite")
            query_params["favorite"] = favorite

    @staticmethod
    def _parse_db_row(db_row: Mapping[str, Any]) -> dict[str, Any]:
        """Parse raw db Mapping into a dict."""
        db_row_dict = MediaControllerBase._parse_db_row(db_row)
        # extract aliases from genre_mappings
        if genre_mappings := db_row_dict.get("genre_mappings"):
            if isinstance(genre_mappings, str):
                genre_mappings = json_loads(genre_mappings)
            if aliases := genre_mappings.get("aliases"):
                db_row_dict["aliases"] = aliases
        # ensure provider_mappings is present (required by model)
        if "provider_mappings" not in db_row_dict:
            db_row_dict["provider_mappings"] = set()
        return db_row_dict

    async def setup(self) -> None:
        """Async initialize of module."""
        # check if we have any genres in the db
        if await self.mass.music.database.get_count(self.db_table) == 0:
            # insert base genres directly
            self.logger.info("Initializing base genres...")
            for genre_name in BASE_GENRES:
                genre_dict = {
                    "name": genre_name,
                    "sort_name": genre_name,
                    "favorite": False,
                    "metadata": "{}",
                    "external_ids": "[]",
                    "play_count": 0,
                    "last_played": 0,
                    "search_name": create_safe_string(genre_name, True, True),
                    "search_sort_name": create_safe_string(genre_name, True, True),
                }
                await self.mass.music.database.insert(self.db_table, genre_dict)
            self.logger.info(f"Initialized {len(BASE_GENRES)} base genres")

    async def _on_item_added(self, event: MassEvent) -> None:
        """Handle event when an item is added to the library."""
        item = event.data
        if not hasattr(item, "metadata") or not item.metadata.genres:
            return
        # trigger a rebuild of the genre index
        # we debounce this to prevent too many rebuilds
        self.mass.call_later(10, self.rebuild_index, task_id="rebuild_genre_index")

    async def rebuild_index(self) -> None:
        """Background task to rebuild the genre index."""
        # wait for the database to be ready
        await asyncio.sleep(5)
        self.logger.info("Starting background genre index scan...")

        # 1. Build a map of all existing genres and aliases
        # map: clean_name -> set[genre_id]
        genre_map: dict[str, set[int]] = {}

        # load all genres
        for genre in await self.library_items(limit=10000):
            clean_name = create_safe_string(genre.name, True, True)
            genre_map.setdefault(clean_name, set()).add(int(genre.item_id))
            # add aliases from genre_mappings
            if hasattr(genre, "aliases") and genre.aliases:
                for alias in genre.aliases:
                    clean_alias = create_safe_string(alias, True, True)
                    genre_map.setdefault(clean_alias, set()).add(int(genre.item_id))

        # 2. Scan all media types
        genre_tracks = await self._scan_media_type(DB_TABLE_TRACKS, genre_map)
        genre_albums = await self._scan_media_type(DB_TABLE_ALBUMS, genre_map)
        genre_artists = await self._scan_media_type(DB_TABLE_ARTISTS, genre_map)
        genre_playlists = await self._scan_media_type(DB_TABLE_PLAYLISTS, genre_map)
        genre_podcasts = await self._scan_media_type(DB_TABLE_PODCASTS, genre_map)
        genre_audiobooks = await self._scan_media_type(DB_TABLE_AUDIOBOOKS, genre_map)

        # 3. Update database
        count = 0
        # get all unique genre ids
        all_genre_ids = (
            set(genre_tracks.keys())
            | set(genre_albums.keys())
            | set(genre_artists.keys())
            | set(genre_playlists.keys())
            | set(genre_podcasts.keys())
            | set(genre_audiobooks.keys())
        )

        for genre_id in all_genre_ids:
            track_ids = list(genre_tracks.get(genre_id, set()))
            album_ids = list(genre_albums.get(genre_id, set()))
            artist_ids = list(genre_artists.get(genre_id, set()))
            playlist_ids = list(genre_playlists.get(genre_id, set()))
            podcast_ids = list(genre_podcasts.get(genre_id, set()))
            audiobook_ids = list(genre_audiobooks.get(genre_id, set()))

            # get existing genre_mappings
            db_row = await self.mass.music.database.get_row(self.db_table, {"item_id": genre_id})
            genre_mappings = {}
            if db_row and db_row["genre_mappings"]:
                genre_mappings = json_loads(db_row["genre_mappings"])

            # update media type IDs
            if track_ids:
                genre_mappings["track_ids"] = track_ids
            if album_ids:
                genre_mappings["album_ids"] = album_ids
            if artist_ids:
                genre_mappings["artist_ids"] = artist_ids
            if playlist_ids:
                genre_mappings["playlist_ids"] = playlist_ids
            if podcast_ids:
                genre_mappings["podcast_ids"] = podcast_ids
            if audiobook_ids:
                genre_mappings["audiobook_ids"] = audiobook_ids

            await self.mass.music.database.update(
                self.db_table,
                {"item_id": genre_id},
                {"genre_mappings": json_dumps(genre_mappings)},
            )

            count += 1
            await asyncio.sleep(0)

        self.logger.info("Finished background genre index scan. Updated %s genres.", count)

    async def _scan_media_type(
        self, table: str, genre_map: dict[str, set[int]]
    ) -> dict[int, set[str]]:
        """Scan a media type table for genres."""
        genre_items: dict[int, set[str]] = {}
        query = (
            f"SELECT item_id, metadata FROM {table} "
            "WHERE json_extract(metadata, '$.genres') IS NOT NULL"
        )

        limit = 500
        offset = 0
        while True:
            rows = await self.mass.music.database.get_rows_from_query(
                query, limit=limit, offset=offset
            )
            if not rows:
                break

            for row in rows:
                try:
                    metadata = json_loads(row["metadata"])
                    if not (genres := metadata.get("genres")):
                        continue
                    item_id = str(row["item_id"])
                    for genre_name in genres:
                        clean_name = create_safe_string(genre_name, True, True)
                        if not (genre_ids := genre_map.get(clean_name)):
                            genre = await self.create(genre_name)
                            genre_ids = {int(genre.item_id)}
                            genre_map[clean_name] = genre_ids
                        for genre_id in genre_ids:
                            if genre_id not in genre_items:
                                genre_items[genre_id] = set()
                            genre_items[genre_id].add(item_id)
                except Exception as err:
                    self.logger.warning(
                        "Error processing item %s from %s for genres: %s",
                        row["item_id"],
                        table,
                        str(err),
                    )

            offset += limit
            await asyncio.sleep(0)  # yield to event loop
        return genre_items

    async def resolve_genre(self, raw_genre_string: str) -> Genre:
        """
        Map a provider genre string to an internal Genre.

        1. Check if raw string matches a Genre Name exactly (case-insensitive).
        2. Check if raw string exists in genre_mappings aliases.
        3. If no match, create new Genre.
        """
        clean_name = raw_genre_string.strip()

        # 1. Check if raw string matches a Genre Name exactly (case-insensitive)
        # we can use the search method for this
        if search_result := await self.search(clean_name, "library", limit=1):
            return search_result[0]

        # 2. Check if raw string exists in genre_mappings aliases
        # We need to search all genres for this alias
        query = (
            f"SELECT item_id FROM {self.db_table} "
            f"WHERE json_extract(genre_mappings, '$.aliases') LIKE '%' || :alias || '%'"
        )
        if rows := await self.mass.music.database.get_rows_from_query(
            query, {"alias": clean_name}, limit=1
        ):
            return await self.get_library_item(rows[0]["item_id"])

        # 3. If no match, create new Genre directly in database
        # We create it as a minimal database record first

        genre_dict = {
            "name": clean_name,
            "sort_name": clean_name,
            "favorite": False,
            "metadata": "{}",
            "external_ids": "[]",
            "play_count": 0,
            "last_played": 0,
            "search_name": create_safe_string(clean_name, True, True),
            "search_sort_name": create_safe_string(clean_name, True, True),
        }
        genre_id = await self.mass.music.database.insert(self.db_table, genre_dict)
        return await self.get_library_item(genre_id)

    async def create(self, name: str) -> Genre:
        """Create a new Genre.

        Note: To add an image to the genre, use the metadata/update_metadata endpoint
        after creation with the returned Genre object.
        """
        return await self.resolve_genre(name)

    async def add_alias(self, genre_id: int, alias: str) -> None:
        """User action: Map a specific string to a specific Genre."""
        # get current genre_mappings
        db_row = await self.mass.music.database.get_row(self.db_table, {"item_id": genre_id})
        if not db_row:
            return

        genre_mappings = {}
        if db_row["genre_mappings"]:
            genre_mappings = json_loads(db_row["genre_mappings"])

        # add alias
        aliases = genre_mappings.get("aliases", [])
        if alias not in aliases:
            aliases.append(alias)
            genre_mappings["aliases"] = aliases

            await self.mass.music.database.update(
                self.db_table,
                {"item_id": genre_id},
                {"genre_mappings": json_dumps(genre_mappings)},
            )

    async def remove_alias(self, alias: str) -> None:
        """User action: Remove a specific alias."""
        # find genre with this alias
        query = (
            f"SELECT item_id, genre_mappings FROM {self.db_table} "
            f"WHERE json_extract(genre_mappings, '$.aliases') LIKE '%' || :alias || '%'"
        )
        rows = await self.mass.music.database.get_rows_from_query(query, {"alias": alias})

        for row in rows:
            genre_mappings = json_loads(row["genre_mappings"])
            aliases = genre_mappings.get("aliases", [])
            if alias in aliases:
                aliases.remove(alias)
                genre_mappings["aliases"] = aliases
                await self.mass.music.database.update(
                    self.db_table,
                    {"item_id": row["item_id"]},
                    {"genre_mappings": json_dumps(genre_mappings)},
                )

    async def remove_item_from_library(
        self,
        item_id: str | int | None = None,
        recursive: bool = True,
        genre_id: str | int | None = None,
        restore_aliases: bool = True,
    ) -> None:
        """Delete record from the database."""
        if item_id is None and genre_id is None:
            raise TypeError("Missing item_id or genre_id")
        db_id = int(item_id if item_id is not None else genre_id)  # type: ignore[arg-type]

        # if restore_aliases is True, we need to fetch the aliases first
        # and create new genres for them
        aliases_to_restore = []
        if restore_aliases:
            db_row = await self.mass.music.database.get_row(self.db_table, {"item_id": db_id})
            if db_row and db_row["genre_mappings"]:
                genre_mappings = json_loads(db_row["genre_mappings"])
                aliases_to_restore = genre_mappings.get("aliases", [])

        # restore aliases as full genres
        for alias in aliases_to_restore:
            await self.create(alias)

        await super().remove_item_from_library(db_id, recursive)

        if aliases_to_restore:
            self.mass.create_task(self.rebuild_index())

    async def merge_genres(self, source_genre_ids: list[int], target_genre_id: int) -> None:
        """Merge multiple genres into one."""
        target_genre = await self.get_library_item(target_genre_id)
        if not target_genre:
            raise MediaNotFoundError(f"Target genre {target_genre_id} not found")

        for source_id in source_genre_ids:
            if source_id == target_genre_id:
                continue
            source_genre = await self.get_library_item(source_id)
            if not source_genre:
                continue

            # 1. Add source name as alias to target
            await self.add_alias(target_genre_id, source_genre.name)

            # 2. Move existing aliases from source to target
            source_db_row = await self.mass.music.database.get_row(
                self.db_table, {"item_id": source_id}
            )
            if source_db_row and source_db_row["genre_mappings"]:
                source_mappings = json_loads(source_db_row["genre_mappings"])
                for alias in source_mappings.get("aliases", []):
                    await self.add_alias(target_genre_id, alias)

            # 3. Move tracks and albums to target
            # This is done by the background scan mostly, but we can force it
            # Actually, since we added the alias, the next scan will link them correctly.
            # But we should probably update the target genre's track_ids/album_ids immediately
            # to reflect the change in the UI.
            # However, the background scan is the source of truth for track/album linking.
            # So we just trigger a rescan of the target genre?
            # Or we just rely on the fact that the source genre is gone and the alias is added.

            # 4. Delete source genre (without restoring aliases!)
            await self.remove_item_from_library(source_id, restore_aliases=False)

        # Trigger a rescan to update links
        self.mass.create_task(self.rebuild_index())

    async def split_genre(self, genre_id: int, alias: str) -> None:
        """Split a genre by removing an alias and creating a new genre from it."""
        # 1. Remove alias from source genre
        await self.remove_alias(alias)

        # 2. Create new genre from alias
        await self.create(alias)

        # 3. Trigger a rescan to update links
        self.mass.create_task(self.rebuild_index())

    async def _add_library_item(
        self,
        item: Genre,
        overwrite_existing: bool = False,
    ) -> int:
        """Add genre to library and return the database id."""
        match = {"name": item.name}
        if db_row := await self.mass.music.database.get_row(self.db_table, match):
            return int(db_row["item_id"])
        item_dict = item.to_dict()
        item_dict.pop("item_id", None)
        return int(await self.mass.music.database.insert(self.db_table, item_dict))

    async def _update_library_item(
        self, item_id: str | int, update: Genre, overwrite: bool = False
    ) -> None:
        """Update existing library record in the database."""
        # Get current item for merge if not overwriting
        db_id = int(item_id)
        cur_item = await self.get_library_item(db_id)

        cur_img_count = (
            len(cur_item.metadata.images) if cur_item.metadata and cur_item.metadata.images else 0
        )
        upd_img_count = (
            len(update.metadata.images) if update.metadata and update.metadata.images else 0
        )
        self.logger.debug(
            f"Updating genre {db_id}: cur_item has {cur_img_count} images, "
            f"update has {upd_img_count} images"
        )

        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)

        final_img_count = len(metadata.images) if metadata and metadata.images else 0
        self.logger.debug(f"After merge, metadata has {final_img_count} images")

        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": update.name if overwrite else cur_item.name,
                "sort_name": update.sort_name if overwrite else cur_item.sort_name,
                "metadata": serialize_to_json(metadata),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
            },
        )

        self.logger.debug(f"Database updated for genre {db_id}")

    async def match_providers(self, db_item: Genre) -> None:
        """
        Try to find match on all (streaming) providers for the provided (database) item.

        This is used to link objects of different providers/qualities together.
        """
        # Genres are not really matched on providers, they are just strings

    async def radio_mode_base_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Get the list of base tracks from the controller used to calculate the dynamic radio."""
        # For genres, we can return random tracks from this genre
        # This is a bit complex as we need to join tracks with genres
        # For now, return empty list or implement a basic query
        # TODO: Implement radio mode for genres
        return []

    async def genre_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
        offset: int = 0,
    ) -> list[Track]:
        """Return tracks for the given genre."""
        if provider_instance_id_or_domain == "library":
            # Get the IDs from the database directly
            db_row = await self.mass.music.database.get_row(
                self.db_table, {"item_id": int(item_id)}
            )
            if not db_row or not (genre_mappings_raw := db_row["genre_mappings"]):
                return []

            genre_mappings = json_loads(genre_mappings_raw)
            track_ids = genre_mappings.get("track_ids", [])
            if not track_ids:
                return []

            ids_str = ",".join(str(x) for x in track_ids)
            return await self.mass.music.tracks.library_items(
                extra_query=f"{DB_TABLE_TRACKS}.item_id IN ({ids_str})",
                limit=limit,
                offset=offset,
            )
        return []

    async def genre_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
        offset: int = 0,
    ) -> list[Album]:
        """Return albums for the given genre."""
        if provider_instance_id_or_domain == "library":
            # Get the IDs from the database directly
            db_row = await self.mass.music.database.get_row(
                self.db_table, {"item_id": int(item_id)}
            )
            if not db_row or not (genre_mappings_raw := db_row["genre_mappings"]):
                return []

            genre_mappings = json_loads(genre_mappings_raw)
            album_ids = genre_mappings.get("album_ids", [])
            if not album_ids:
                return []

            ids_str = ",".join(str(x) for x in album_ids)
            return await self.mass.music.albums.library_items(
                extra_query=f"{DB_TABLE_ALBUMS}.item_id IN ({ids_str})",
                limit=limit,
                offset=offset,
            )
        return []

    async def genre_artists(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
        offset: int = 0,
    ) -> list[Artist]:
        """Return artists for the given genre."""
        if provider_instance_id_or_domain == "library":
            # Get the IDs from the database directly
            db_row = await self.mass.music.database.get_row(
                self.db_table, {"item_id": int(item_id)}
            )
            if not db_row or not (genre_mappings_raw := db_row["genre_mappings"]):
                return []

            genre_mappings = json_loads(genre_mappings_raw)
            artist_ids = genre_mappings.get("artist_ids", [])
            if not artist_ids:
                return []

            ids_str = ",".join(str(x) for x in artist_ids)
            return await self.mass.music.artists.library_items(
                extra_query=f"{DB_TABLE_ARTISTS}.item_id IN ({ids_str})",
                limit=limit,
                offset=offset,
            )
        return []

    async def genre_playlists(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
        offset: int = 0,
    ) -> list[Playlist]:
        """Return playlists for the given genre."""
        if provider_instance_id_or_domain == "library":
            # Get the IDs from the database directly
            db_row = await self.mass.music.database.get_row(
                self.db_table, {"item_id": int(item_id)}
            )
            if not db_row or not (genre_mappings_raw := db_row["genre_mappings"]):
                return []

            genre_mappings = json_loads(genre_mappings_raw)
            playlist_ids = genre_mappings.get("playlist_ids", [])
            if not playlist_ids:
                return []

            ids_str = ",".join(str(x) for x in playlist_ids)
            return await self.mass.music.playlists.library_items(
                extra_query=f"{DB_TABLE_PLAYLISTS}.item_id IN ({ids_str})",
                limit=limit,
                offset=offset,
            )
        return []

    async def genre_podcasts(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
        offset: int = 0,
    ) -> list[Podcast]:
        """Return podcasts for the given genre."""
        if provider_instance_id_or_domain == "library":
            # Get the IDs from the database directly
            db_row = await self.mass.music.database.get_row(
                self.db_table, {"item_id": int(item_id)}
            )
            if not db_row or not (genre_mappings_raw := db_row["genre_mappings"]):
                return []

            genre_mappings = json_loads(genre_mappings_raw)
            podcast_ids = genre_mappings.get("podcast_ids", [])
            if not podcast_ids:
                return []

            ids_str = ",".join(str(x) for x in podcast_ids)
            return await self.mass.music.podcasts.library_items(
                extra_query=f"{DB_TABLE_PODCASTS}.item_id IN ({ids_str})",
                limit=limit,
                offset=offset,
            )
        return []

    async def genre_audiobooks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
        offset: int = 0,
    ) -> list[Audiobook]:
        """Return audiobooks for the given genre."""
        if provider_instance_id_or_domain == "library":
            # Get the IDs from the database directly
            db_row = await self.mass.music.database.get_row(
                self.db_table, {"item_id": int(item_id)}
            )
            if not db_row or not (genre_mappings_raw := db_row["genre_mappings"]):
                return []

            genre_mappings = json_loads(genre_mappings_raw)
            audiobook_ids = genre_mappings.get("audiobook_ids", [])
            if not audiobook_ids:
                return []

            ids_str = ",".join(str(x) for x in audiobook_ids)
            return await self.mass.music.audiobooks.library_items(
                extra_query=f"{DB_TABLE_AUDIOBOOKS}.item_id IN ({ids_str})",
                limit=limit,
                offset=offset,
            )
        return []
