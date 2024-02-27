"""MusicController: Orchestrates all data from music providers and sync to internal database."""

from __future__ import annotations

import asyncio
import os
import shutil
import statistics
from collections.abc import AsyncGenerator
from contextlib import suppress
from itertools import zip_longest
from typing import TYPE_CHECKING

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import json_dumps, json_loads
from music_assistant.common.helpers.uri import parse_uri
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import (
    ConfigEntryType,
    EventType,
    ExternalID,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant.common.models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant.common.models.media_items import BrowseFolder, MediaItemType, SearchResults
from music_assistant.common.models.provider import SyncTask
from music_assistant.constants import (
    DB_SCHEMA_VERSION,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_RADIOS,
    DB_TABLE_SETTINGS,
    DB_TABLE_TRACK_LOUDNESS,
    DB_TABLE_TRACKS,
)
from music_assistant.server.helpers.api import api_command
from music_assistant.server.helpers.database import DatabaseConnection
from music_assistant.server.models.core_controller import CoreController

from .media.albums import AlbumsController
from .media.artists import ArtistsController
from .media.playlists import PlaylistController
from .media.radio import RadioController
from .media.tracks import TracksController

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import CoreConfig
    from music_assistant.server.models.music_provider import MusicProvider

DEFAULT_SYNC_INTERVAL = 3 * 60  # default sync interval in minutes
CONF_SYNC_INTERVAL = "sync_interval"


class MusicController(CoreController):
    """Several helpers around the musicproviders."""

    domain: str = "music"
    database: DatabaseConnection | None = None
    config: CoreConfig

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self.cache = self.mass.cache
        self.artists = ArtistsController(self.mass)
        self.albums = AlbumsController(self.mass)
        self.tracks = TracksController(self.mass)
        self.radio = RadioController(self.mass)
        self.playlists = PlaylistController(self.mass)
        self.in_progress_syncs: list[SyncTask] = []
        self._sync_lock = asyncio.Lock()
        self.manifest.name = "Music controller"
        self.manifest.description = (
            "Music Assistant's core controller which manages all music from all providers."
        )
        self.manifest.icon = "archive-music"
        self._sync_task: asyncio.Task | None = None

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        return (
            ConfigEntry(
                key=CONF_SYNC_INTERVAL,
                type=ConfigEntryType.INTEGER,
                range=(5, 720),
                default_value=DEFAULT_SYNC_INTERVAL,
                label="Sync interval",
                description="Interval (in minutes) that a (delta) sync "
                "of all providers should be performed.",
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self.config = config
        # setup library database
        await self._setup_database()
        sync_interval = config.get_value(CONF_SYNC_INTERVAL)
        self.logger.info("Using a sync interval of %s minutes.", sync_interval)
        self._schedule_sync()

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
        await self.database.close()

    @property
    def providers(self) -> list[MusicProvider]:
        """Return all loaded/running MusicProviders (instances)."""
        return self.mass.get_providers(ProviderType.MUSIC)

    @api_command("music/sync")
    def start_sync(
        self,
        media_types: list[MediaType] | None = None,
        providers: list[str] | None = None,
    ) -> None:
        """Start running the sync of (all or selected) musicproviders.

        media_types: only sync these media types. None for all.
        providers: only sync these provider instances. None for all.
        """
        if media_types is None:
            media_types = MediaType.ALL
        if providers is None:
            providers = [x.instance_id for x in self.providers]

        for provider in self.providers:
            if provider.instance_id not in providers:
                continue
            self._start_provider_sync(provider.instance_id, media_types)

    @api_command("music/synctasks")
    def get_running_sync_tasks(self) -> list[SyncTask]:
        """Return list with providers that are currently (scheduled for) syncing."""
        return self.in_progress_syncs

    @api_command("music/search")
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] = MediaType.ALL,
        limit: int = 50,
    ) -> SearchResults:
        """Perform global search for media items on all providers.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: number of items to return in the search (per type).
        """
        # include results from all (unique) music providers
        results_per_provider: list[SearchResults] = await asyncio.gather(
            *[
                self.search_provider(
                    search_query,
                    provider_instance,
                    media_types,
                    limit=limit,
                )
                for provider_instance in self.get_unique_providers()
            ]
        )
        # return result from all providers while keeping index
        # so the result is sorted as each provider delivered
        return SearchResults(
            artists=[
                item
                for sublist in zip_longest(*[x.artists for x in results_per_provider])
                for item in sublist
                if item is not None
            ],
            albums=[
                item
                for sublist in zip_longest(*[x.albums for x in results_per_provider])
                for item in sublist
                if item is not None
            ],
            tracks=[
                item
                for sublist in zip_longest(*[x.tracks for x in results_per_provider])
                for item in sublist
                if item is not None
            ],
            playlists=[
                item
                for sublist in zip_longest(*[x.playlists for x in results_per_provider])
                for item in sublist
                if item is not None
            ],
            radio=[
                item
                for sublist in zip_longest(*[x.radio for x in results_per_provider])
                for item in sublist
                if item is not None
            ],
        )

    async def search_provider(
        self,
        search_query: str,
        provider_instance_id_or_domain: str,
        media_types: list[MediaType] = MediaType.ALL,
        limit: int = 10,
    ) -> SearchResults:
        """Perform search on given provider.

        :param search_query: Search query
        :param provider_instance_id_or_domain: instance_id or domain of the provider
                                               to perform the search on.
        :param provider_instance: instance id of the provider to perform the search on.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: number of items to return in the search (per type).
        """
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if not prov:
            return SearchResults()
        if ProviderFeature.SEARCH not in prov.supported_features:
            return SearchResults()

        # create safe search string
        search_query = search_query.replace("/", " ").replace("'", "")

        # prefer cache items (if any)
        media_types_str = ",".join(media_types)
        cache_key = f"{prov.instance_id}.search.{search_query}.{limit}.{media_types_str}"
        cache_key += "".join(x for x in media_types)

        if prov.is_streaming_provider and (cache := await self.mass.cache.get(cache_key)):
            return SearchResults.from_dict(cache)
        # no items in cache - get listing from provider
        result = await prov.search(
            search_query,
            media_types,
            limit,
        )
        # store (serializable items) in cache
        if prov.is_streaming_provider:
            self.mass.create_task(
                self.mass.cache.set(cache_key, result.to_dict(), expiration=86400 * 7)
            )
        return result

    @api_command("music/browse")
    async def browse(self, path: str | None = None) -> AsyncGenerator[MediaItemType, None]:
        """Browse Music providers."""
        if not path or path == "root":
            # root level; folder per provider
            for prov in self.providers:
                if ProviderFeature.BROWSE not in prov.supported_features:
                    continue
                yield BrowseFolder(
                    item_id="root",
                    provider=prov.domain,
                    path=f"{prov.instance_id}://",
                    uri=f"{prov.instance_id}://",
                    name=prov.name,
                )
            return

        # provider level
        provider_instance, sub_path = path.split("://", 1)
        prov = self.mass.get_provider(provider_instance)
        # handle regular provider listing, always add back folder first
        if not prov or not sub_path:
            yield BrowseFolder(item_id="root", provider="library", path="root", name="..")
        else:
            back_path = f"{provider_instance}://" + "/".join(sub_path.split("/")[:-1])
            yield BrowseFolder(
                item_id="back", provider=provider_instance, path=back_path, name=".."
            )
        async for item in prov.browse(path):
            yield item

    @api_command("music/recently_played_items")
    async def recently_played(
        self, limit: int = 10, media_types: list[MediaType] | None = None
    ) -> list[MediaItemType]:
        """Return a list of the last played items."""
        if media_types is None:
            media_types = [MediaType.TRACK, MediaType.RADIO]
        media_types_str = "(" + ",".join(f'"{x}"' for x in media_types) + ")"
        query = (
            f"SELECT * FROM {DB_TABLE_PLAYLOG} WHERE media_type "
            f"in {media_types_str} ORDER BY timestamp DESC"
        )
        db_rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)
        result: list[MediaItemType] = []
        for db_row in db_rows:
            with suppress(MediaNotFoundError):
                media_type = MediaType(db_row["media_type"])
                item = await self.get_item(media_type, db_row["item_id"], db_row["provider"])
                result.append(item)
        return result

    @api_command("music/item_by_uri")
    async def get_item_by_uri(self, uri: str) -> MediaItemType:
        """Fetch MediaItem by uri."""
        media_type, provider_instance_id_or_domain, item_id = parse_uri(uri)
        return await self.get_item(
            media_type=media_type,
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
        )

    @api_command("music/item")
    async def get_item(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        lazy: bool = True,
        add_to_library: bool = False,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        if provider_instance_id_or_domain == "database":
            # backwards compatibility - to remove when 2.0 stable is released
            provider_instance_id_or_domain = "library"
        if provider_instance_id_or_domain == "url":
            # handle special case of 'URL' MusicProvider which allows us to play regular url's
            return await self.mass.get_provider("url").parse_item(item_id)
        ctrl = self.get_controller(media_type)
        return await ctrl.get(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            force_refresh=force_refresh,
            lazy=lazy,
            add_to_library=add_to_library,
        )

    @api_command("music/favorites/add_item")
    async def add_item_to_favorites(
        self,
        item: str | MediaItemType,
    ) -> None:
        """Add an item to the favorites."""
        if isinstance(item, str):
            item = await self.get_item_by_uri(item)
        # make sure we have a full library item
        # a favorite must always be in the library
        full_item = await self.get_item(
            item.media_type,
            item.item_id,
            item.provider,
            lazy=False,
            add_to_library=True,
        )
        # set favorite in library db
        ctrl = self.get_controller(item.media_type)
        await ctrl.set_favorite(
            full_item.item_id,
            True,
        )

    @api_command("music/favorites/remove_item")
    async def remove_item_from_favorites(
        self,
        media_type: MediaType,
        library_item_id: str | int,
    ) -> None:
        """Remove (library) item from the favorites."""
        ctrl = self.get_controller(media_type)
        await ctrl.set_favorite(
            library_item_id,
            False,
        )

    @api_command("music/library/remove_item")
    async def remove_item_from_library(
        self, media_type: MediaType, library_item_id: str | int
    ) -> None:
        """
        Remove item from the library.

        Destructive! Will remove the item and all dependants.
        """
        ctrl = self.get_controller(media_type)
        item = await ctrl.get_library_item(library_item_id)
        # remove from all providers
        for provider_mapping in item.provider_mappings:
            prov_controller = self.mass.get_provider(provider_mapping.provider_instance)
            with suppress(NotImplementedError):
                await prov_controller.library_remove(provider_mapping.item_id, item.media_type)
        await ctrl.remove_item_from_library(library_item_id)

    @api_command("music/library/add_item")
    async def add_item_to_library(self, item: str | MediaItemType) -> MediaItemType:
        """Add item (uri or mediaitem) to the library."""
        if isinstance(item, str):
            item = await self.get_item_by_uri(item)
        ctrl = self.get_controller(item.media_type)
        # add to provider's library first
        provider = self.mass.get_provider(item.provider)
        if provider.library_edit_supported(item.media_type):
            await provider.library_add(item.item_id, item.media_type)
        return await ctrl.get(
            item_id=item.item_id,
            provider_instance_id_or_domain=item.provider,
            details=item,
            add_to_library=True,
        )

    async def refresh_items(self, items: list[MediaItemType]) -> None:
        """Refresh MediaItems to force retrieval of full info and matches.

        Creates background tasks to process the action.
        """
        for media_item in items:
            self.mass.create_task(self.refresh_item(media_item))

    @api_command("music/refresh_item")
    async def refresh_item(
        self,
        media_item: MediaItemType,
    ) -> MediaItemType | None:
        """Try to refresh a mediaitem by requesting it's full object or search for substitutes."""
        try:
            return await self.get_item(
                media_item.media_type,
                media_item.item_id,
                media_item.provider,
                force_refresh=True,
                lazy=False,
                add_to_library=True,
            )
        except MusicAssistantError:
            pass

        searchresult = await self.search(media_item.name, [media_item.media_type], 20)
        if media_item.media_type == MediaType.ARTIST:
            result = searchresult.artists
        elif media_item.media_type == MediaType.ALBUM:
            result = searchresult.albums
        elif media_item.media_type == MediaType.TRACK:
            result = searchresult.tracks
        elif media_item.media_type == MediaType.PLAYLIST:
            result = searchresult.playlists
        else:
            result = searchresult.radio
        for item in result:
            if item.available:
                return await self.get_item(
                    item.media_type,
                    item.item_id,
                    item.provider,
                    lazy=False,
                    add_to_library=True,
                )
        return None

    async def set_track_loudness(
        self, item_id: str, provider_instance_id_or_domain: str, loudness: int
    ) -> None:
        """List integrated loudness for a track in db."""
        await self.database.insert(
            DB_TABLE_TRACK_LOUDNESS,
            {
                "item_id": item_id,
                "provider": provider_instance_id_or_domain,
                "loudness": loudness,
            },
            allow_replace=True,
        )

    async def get_track_loudness(
        self, item_id: str, provider_instance_id_or_domain: str
    ) -> float | None:
        """Get integrated loudness for a track in db."""
        if result := await self.database.get_row(
            DB_TABLE_TRACK_LOUDNESS,
            {
                "item_id": item_id,
                "provider": provider_instance_id_or_domain,
            },
        ):
            return result["loudness"]
        return None

    async def get_provider_loudness(self, provider_instance_id_or_domain: str) -> float | None:
        """Get average integrated loudness for tracks of given provider."""
        all_items = []
        if provider_instance_id_or_domain == "url":
            # this is not a very good idea for random urls
            return None
        for db_row in await self.database.get_rows(
            DB_TABLE_TRACK_LOUDNESS,
            {
                "provider": provider_instance_id_or_domain,
            },
        ):
            all_items.append(db_row["loudness"])
        if all_items:
            return statistics.fmean(all_items)
        return None

    async def mark_item_played(
        self, media_type: MediaType, item_id: str, provider_instance_id_or_domain: str
    ) -> None:
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()
        await self.database.insert(
            DB_TABLE_PLAYLOG,
            {
                "item_id": item_id,
                "provider": provider_instance_id_or_domain,
                "media_type": media_type.value,
                "timestamp": timestamp,
            },
            allow_replace=True,
        )

    def get_controller(
        self, media_type: MediaType
    ) -> (
        ArtistsController
        | AlbumsController
        | TracksController
        | RadioController
        | PlaylistController
    ):
        """Return controller for MediaType."""
        if media_type == MediaType.ARTIST:
            return self.artists
        if media_type == MediaType.ALBUM:
            return self.albums
        if media_type == MediaType.TRACK:
            return self.tracks
        if media_type == MediaType.RADIO:
            return self.radio
        if media_type == MediaType.PLAYLIST:
            return self.playlists
        return None

    def get_unique_providers(self) -> set[str]:
        """
        Return all unique MusicProvider instance ids.

        This will return all filebased instances but only one instance
        for streaming providers.
        """
        instances = set()
        domains = set()
        for provider in self.providers:
            if provider.domain not in domains or not provider.is_streaming_provider:
                instances.add(provider.instance_id)
                domains.add(provider.domain)
        return instances

    def _start_provider_sync(
        self, provider_instance: str, media_types: tuple[MediaType, ...]
    ) -> None:
        """Start sync task on provider and track progress."""
        # check if we're not already running a sync task for this provider/mediatype
        for sync_task in self.in_progress_syncs:
            if sync_task.provider_instance != provider_instance:
                continue
            for media_type in media_types:
                if media_type in sync_task.media_types:
                    self.logger.debug(
                        "Skip sync task for %s because another task is already in progress",
                        provider_instance,
                    )
                    return

        provider = self.mass.get_provider(provider_instance)

        async def run_sync() -> None:
            # Wrap the provider sync into a lock to prevent
            # race conditions when multiple providers are syncing at the same time.
            async with self._sync_lock:
                await provider.sync_library(media_types)

        # we keep track of running sync tasks
        task = self.mass.create_task(run_sync())
        sync_spec = SyncTask(
            provider_domain=provider.domain,
            provider_instance=provider.instance_id,
            media_types=media_types,
            task=task,
        )
        self.in_progress_syncs.append(sync_spec)

        self.mass.signal_event(EventType.SYNC_TASKS_UPDATED, data=self.in_progress_syncs)

        def on_sync_task_done(task: asyncio.Task) -> None:
            self.in_progress_syncs.remove(sync_spec)
            if task.cancelled():
                return
            if task_err := task.exception():
                self.logger.warning(
                    "Sync task for %s completed with errors",
                    provider.name,
                    exc_info=task_err if self.logger.isEnabledFor(10) else None,
                )
            else:
                self.logger.info("Sync task for %s completed", provider.name)
            self.mass.signal_event(EventType.SYNC_TASKS_UPDATED, data=self.in_progress_syncs)
            # trigger metadata scan after all provider syncs completed
            if len(self.in_progress_syncs) == 0:
                self.mass.metadata.start_scan()

        task.add_done_callback(on_sync_task_done)

    async def cleanup_provider(self, provider_instance: str) -> None:
        """Cleanup provider records from the database."""
        # clean cache items from deleted provider(s)
        await self.mass.cache.clear(provider_instance)

        # cleanup media items from db matched to deleted provider
        for ctrl in (
            # order is important here to recursively cleanup bottom up
            self.mass.music.radio,
            self.mass.music.playlists,
            self.mass.music.tracks,
            self.mass.music.albums,
            self.mass.music.artists,
        ):
            prov_items = await ctrl.get_library_items_by_prov_id(provider_instance)
            for item in prov_items:
                await ctrl.remove_provider_mappings(item.item_id, provider_instance)

    def _schedule_sync(self) -> None:
        """Schedule the periodic sync."""
        self.start_sync()
        sync_interval = self.config.get_value(CONF_SYNC_INTERVAL)
        # we reschedule ourselves right after execution
        # NOTE: sync_interval is stored in minutes, we need seconds
        self.mass.loop.call_later(sync_interval * 60, self._schedule_sync)

    async def _setup_database(self) -> None:
        """Initialize database."""
        db_path = os.path.join(self.mass.storage_path, "library.db")
        self.database = DatabaseConnection(db_path)
        await self.database.setup()

        # always create db tables if they don't exist to prevent errors trying to access them later
        await self.__create_database_tables()
        try:
            if db_row := await self.database.get_row(DB_TABLE_SETTINGS, {"key": "version"}):
                prev_version = int(db_row["value"])
            else:
                prev_version = 0
        except (KeyError, ValueError):
            prev_version = 0

        if prev_version not in (0, DB_SCHEMA_VERSION):
            # db version mismatch - we need to do a migration
            # make a backup of db file
            db_path_backup = db_path + ".backup"
            await asyncio.to_thread(shutil.copyfile, db_path, db_path_backup)

            # handle db migration from previous schema to this one
            if prev_version == 25:
                self.logger.info(
                    "Performing database migration from %s to %s",
                    prev_version,
                    DB_SCHEMA_VERSION,
                )
                self.logger.warning("DATABASE MIGRATION IN PROGRESS - THIS CAN TAKE A WHILE")
                # migrate external id(s)
                for table in (
                    DB_TABLE_ARTISTS,
                    DB_TABLE_ALBUMS,
                    DB_TABLE_TRACKS,
                    DB_TABLE_PLAYLISTS,
                    DB_TABLE_RADIOS,
                ):
                    # create new external_ids column
                    await self.database.execute(
                        f"ALTER TABLE {table} "
                        "ADD COLUMN external_ids "
                        "json NOT NULL DEFAULT '[]'"
                    )
                    if table in (DB_TABLE_PLAYLISTS, DB_TABLE_RADIOS):
                        continue
                    # migrate existing ids into the new external_ids column
                    async for item in self.database.iter_items(table):
                        external_ids: set[tuple[str, str]] = set()
                        if mbid := item["mbid"]:
                            external_ids.add((ExternalID.MUSICBRAINZ, mbid))
                        for prov_mapping in json_loads(item["provider_mappings"]):
                            if isrc := prov_mapping.get("isrc"):
                                external_ids.add((ExternalID.ISRC, isrc))
                            if barcode := prov_mapping.get("barcode"):
                                external_ids.add((ExternalID.BARCODE, barcode))
                        if external_ids:
                            await self.database.update(
                                table,
                                {
                                    "item_id": item["item_id"],
                                },
                                {
                                    "external_ids": json_dumps(external_ids),
                                },
                            )
                    # drop mbid column
                    await self.database.execute(f"DROP INDEX IF EXISTS {table}_mbid_idx")
                    await self.database.execute(f"ALTER TABLE {table} DROP COLUMN mbid")
                # db migration succeeded
                self.logger.info(
                    "Database migration to version %s completed",
                    DB_SCHEMA_VERSION,
                )
            # handle all other schema versions
            else:
                # we keep it simple and just recreate the tables
                # if the schema is too old (or too new)
                # we do migrations only for up to 1 schema version behind
                self.logger.warning(
                    "Database schema too old - Resetting library/database - "
                    "a full rescan will be performed!"
                )
                for table in (
                    DB_TABLE_TRACKS,
                    DB_TABLE_ALBUMS,
                    DB_TABLE_ARTISTS,
                    DB_TABLE_TRACKS,
                    DB_TABLE_PLAYLISTS,
                    DB_TABLE_RADIOS,
                    DB_TABLE_PROVIDER_MAPPINGS,
                ):
                    await self.database.execute(f"DROP TABLE IF EXISTS {table}")
                # recreate missing tables
                await self.__create_database_tables()

        # store current schema version
        await self.database.insert_or_replace(
            DB_TABLE_SETTINGS,
            {"key": "version", "value": str(DB_SCHEMA_VERSION), "type": "str"},
        )
        # create indexes if needed
        await self.__create_database_indexes()
        # compact db
        self.logger.debug("Compacting database...")
        await self.database.vacuum()
        self.logger.debug("Compacting database done")

    async def __create_database_tables(self) -> None:
        """Create database tables."""
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_SETTINGS}(
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    type TEXT
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_TRACK_LOUDNESS}(
                    item_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    loudness REAL,
                    UNIQUE(item_id, provider));"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_PLAYLOG}(
                item_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'track',
                timestamp INTEGER DEFAULT 0,
                UNIQUE(item_id, provider, media_type));"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUMS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    sort_artist TEXT,
                    album_type TEXT NOT NULL,
                    year INTEGER,
                    version TEXT,
                    favorite BOOLEAN DEFAULT 0,
                    artists json NOT NULL,
                    metadata json NOT NULL,
                    provider_mappings json NOT NULL,
                    external_ids json NOT NULL,
                    timestamp_added INTEGER NOT NULL,
                    timestamp_modified INTEGER NOT NULL
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ARTISTS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    favorite BOOLEAN DEFAULT 0,
                    metadata json NOT NULL,
                    provider_mappings json NOT NULL,
                    external_ids json NOT NULL,
                    timestamp_added INTEGER NOT NULL,
                    timestamp_modified INTEGER NOT NULL
                    );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_TRACKS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    sort_artist TEXT,
                    version TEXT,
                    duration INTEGER,
                    favorite BOOLEAN DEFAULT 0,
                    artists json NOT NULL,
                    metadata json NOT NULL,
                    provider_mappings json NOT NULL,
                    external_ids json NOT NULL,
                    timestamp_added INTEGER NOT NULL,
                    timestamp_modified INTEGER NOT NULL
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUM_TRACKS}(
                    track_id INTEGER NOT NULL,
                    album_id INTEGER NOT NULL,
                    disc_number INTEGER NOT NULL,
                    track_number INTEGER NOT NULL,
                    UNIQUE(track_id, album_id)
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_PLAYLISTS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    is_editable BOOLEAN NOT NULL,
                    favorite BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_mappings json,
                    external_ids json NOT NULL,
                    timestamp_added INTEGER NOT NULL,
                    timestamp_modified INTEGER NOT NULL
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_RADIOS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    sort_name TEXT NOT NULL,
                    favorite BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_mappings json,
                    external_ids json NOT NULL,
                    timestamp_added INTEGER NOT NULL,
                    timestamp_modified INTEGER NOT NULL
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_PROVIDER_MAPPINGS}(
                    media_type TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    provider_domain TEXT NOT NULL,
                    provider_instance TEXT NOT NULL,
                    provider_item_id TEXT NOT NULL,
                    UNIQUE(media_type, provider_instance, provider_item_id)
                );"""
        )

    async def __create_database_indexes(self) -> None:
        """Create database indexes."""
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS artists_in_library_idx on artists(favorite);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS albums_in_library_idx on albums(favorite);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS tracks_in_library_idx on tracks(favorite);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS playlists_in_library_idx on playlists(favorite);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS radios_in_library_idx on radios(favorite);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS artists_sort_name_idx on artists(sort_name);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS albums_sort_name_idx on albums(sort_name);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS tracks_sort_name_idx on tracks(sort_name);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS playlists_sort_name_idx on playlists(sort_name);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS radios_sort_name_idx on radios(sort_name);"
        )
