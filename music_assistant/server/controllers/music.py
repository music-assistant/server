"""MusicController: Orchestrates all data from music providers and sync to internal database."""
from __future__ import annotations

import asyncio
import logging
import os
import statistics
from itertools import zip_longest
from typing import TYPE_CHECKING

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.uri import parse_uri
from music_assistant.common.models.enums import EventType, MediaType, ProviderFeature, ProviderType
from music_assistant.common.models.errors import MusicAssistantError
from music_assistant.common.models.media_items import BrowseFolder, MediaItemType, SearchResults
from music_assistant.common.models.provider import SyncTask
from music_assistant.constants import (
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_RADIOS,
    DB_TABLE_SETTINGS,
    DB_TABLE_TRACK_LOUDNESS,
    DB_TABLE_TRACKS,
    ROOT_LOGGER_NAME,
    SCHEMA_VERSION,
)
from music_assistant.server.helpers.api import api_command
from music_assistant.server.helpers.database import DatabaseConnection
from music_assistant.server.models.music_provider import MusicProvider

from .media.albums import AlbumsController
from .media.artists import ArtistsController
from .media.playlists import PlaylistController
from .media.radio import RadioController
from .media.tracks import TracksController

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.music")
SYNC_INTERVAL = 3 * 3600


class MusicController:
    """Several helpers around the musicproviders."""

    database: DatabaseConnection | None = None

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.artists = ArtistsController(mass)
        self.albums = AlbumsController(mass)
        self.tracks = TracksController(mass)
        self.radio = RadioController(mass)
        self.playlists = PlaylistController(mass)
        self.in_progress_syncs: list[SyncTask] = []
        self._sync_lock = asyncio.Lock()

    async def setup(self):
        """Async initialize of module."""
        # setup library database
        await self._setup_database()
        self.mass.create_task(self.start_sync(reschedule=SYNC_INTERVAL))

    async def close(self) -> None:
        """Cleanup on exit."""
        await self.database.close()

    @property
    def providers(self) -> list[MusicProvider]:
        """Return all loaded/running MusicProviders (instances)."""
        return self.mass.get_providers(ProviderType.MUSIC)

    @api_command("music/sync")
    async def start_sync(
        self,
        media_types: list[MediaType] | None = None,
        providers: list[str] | None = None,
        reschedule: int | None = None,
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

        # reschedule task if needed
        def create_sync_task():
            self.mass.create_task(self.start_sync, media_types, providers, reschedule)

        if reschedule is not None:
            self.mass.loop.call_later(reschedule, create_sync_task)

    @api_command("music/synctasks")
    def get_running_sync_tasks(self) -> list[SyncTask]:
        """Return list with providers that are currently (scheduled for) syncing."""
        return self.in_progress_syncs

    @api_command("music/search")
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] = MediaType.ALL,
        limit: int = 10,
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

        if cache := await self.mass.cache.get(cache_key):
            return SearchResults.from_dict(cache)
        # no items in cache - get listing from provider
        result = await prov.search(
            search_query,
            media_types,
            limit,
        )
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(cache_key, result.to_dict(), expiration=86400 * 7)
        )
        return result

    @api_command("music/browse")
    async def browse(self, path: str | None = None) -> BrowseFolder:
        """Browse Music providers."""
        # root level; folder per provider
        if not path or path == "root":
            return BrowseFolder(
                item_id="root",
                provider="database",
                path="root",
                label="browse",
                name="",
                items=[
                    BrowseFolder(
                        item_id="root",
                        provider=prov.domain,
                        path=f"{prov.instance_id}://",
                        uri=f"{prov.instance_id}://",
                        name=prov.name,
                    )
                    for prov in self.providers
                    if ProviderFeature.BROWSE in prov.supported_features
                ],
            )
        # provider level
        provider_instance = path.split("://", 1)[0]
        prov = self.mass.get_provider(provider_instance)
        return await prov.browse(path)

    @api_command("music/item_by_uri")
    async def get_item_by_uri(
        self, uri: str, force_refresh: bool = False, lazy: bool = True
    ) -> MediaItemType:
        """Fetch MediaItem by uri."""
        media_type, provider_instance_id_or_domain, item_id = parse_uri(uri)
        return await self.get_item(
            media_type=media_type,
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            force_refresh=force_refresh,
            lazy=lazy,
        )

    @api_command("music/item")
    async def get_item(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        lazy: bool = True,
        add_to_db: bool = False,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        if provider_instance_id_or_domain == "url":
            # handle special case of 'URL' MusicProvider which allows us to play regular url's
            return await self.mass.get_provider("url").parse_item(item_id)
        ctrl = self.get_controller(media_type)
        return await ctrl.get(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            force_refresh=force_refresh,
            lazy=lazy,
            add_to_db=add_to_db,
        )

    @api_command("music/library/add")
    async def add_to_library(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> None:
        """Add an item to the library."""
        # make sure we have a full db item
        full_item = await self.get_item(
            media_type,
            item_id,
            provider_instance_id_or_domain,
            lazy=False,
            add_to_db=True,
        )
        ctrl = self.get_controller(media_type)
        await ctrl.add_to_library(
            full_item.item_id,
            full_item.provider,
        )

    @api_command("music/library/add_items")
    async def add_items_to_library(self, items: list[str | MediaItemType]) -> None:
        """Add multiple items to the library (provide uri or MediaItem)."""
        tasks = []
        for item in items:
            if isinstance(item, str):
                item = await self.get_item_by_uri(item)  # noqa: PLW2901
            tasks.append(
                self.mass.create_task(
                    self.add_to_library(
                        media_type=item.media_type,
                        item_id=item.item_id,
                        provider_instance_id_or_domain=item.provider,
                    )
                )
            )
        await asyncio.gather(*tasks)

    @api_command("music/library/remove")
    async def remove_from_library(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> None:
        """Remove item from the library."""
        ctrl = self.get_controller(media_type)
        await ctrl.remove_from_library(
            item_id,
            provider_instance_id_or_domain,
        )

    @api_command("music/library/remove_items")
    async def remove_items_from_library(self, items: list[str | MediaItemType]) -> None:
        """Remove multiple items from the library (provide uri or MediaItem)."""
        tasks = []
        for item in items:
            if isinstance(item, str):
                item = await self.get_item_by_uri(item)  # noqa: PLW2901
            tasks.append(
                self.mass.create_task(
                    self.remove_from_library(
                        media_type=item.media_type,
                        item_id=item.item_id,
                        provider_instance_id_or_domain=item.provider,
                    )
                )
            )
        await asyncio.gather(*tasks)

    @api_command("music/delete")
    async def delete(
        self, media_type: MediaType, db_item_id: str | int, recursive: bool = False
    ) -> None:
        """Remove item from the database."""
        ctrl = self.get_controller(media_type)
        await ctrl.delete(db_item_id, recursive)

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
                add_to_db=True,
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
                    item.media_type, item.item_id, item.provider, lazy=False, add_to_db=True
                )
        return None

    async def set_track_loudness(
        self, item_id: str, provider_instance_id_or_domain: str, loudness: int
    ):
        """List integrated loudness for a track in db."""
        await self.database.insert(
            DB_TABLE_TRACK_LOUDNESS,
            {"item_id": item_id, "provider": provider_instance_id_or_domain, "loudness": loudness},
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

    async def mark_item_played(self, item_id: str, provider_instance_id_or_domain: str):
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()
        await self.database.insert(
            DB_TABLE_PLAYLOG,
            {
                "item_id": item_id,
                "provider": provider_instance_id_or_domain,
                "timestamp": timestamp,
            },
            allow_replace=True,
        )

    async def library_add_items(self, items: list[MediaItemType]) -> None:
        """Add media item(s) to the library.

        Creates background tasks to process the action.
        """
        for media_item in items:
            self.mass.create_task(
                self.add_to_library(
                    media_item.media_type,
                    media_item.item_id,
                    media_item.provider,
                )
            )

    async def library_remove_items(self, items: list[MediaItemType]) -> None:
        """Remove media item(s) from the library.

        Creates background tasks to process the action.
        """
        for media_item in items:
            self.mass.create_task(
                self.remove_from_library(
                    media_item.media_type,
                    media_item.item_id,
                    media_item.provider,
                )
            )

    def get_controller(
        self, media_type: MediaType
    ) -> (
        ArtistsController
        | AlbumsController
        | TracksController
        | RadioController
        | PlaylistController
    ):  # noqa: E501
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
            if provider.domain not in domains or provider.is_unique:
                instances.add(provider.instance_id)
                domains.add(provider.domain)
        return instances

    def _start_provider_sync(self, provider_instance: str, media_types: tuple[MediaType, ...]):
        """Start sync task on provider and track progress."""
        # check if we're not already running a sync task for this provider/mediatype
        for sync_task in self.in_progress_syncs:
            if sync_task.provider_instance != provider_instance:
                continue
            for media_type in media_types:
                if media_type in sync_task.media_types:
                    LOGGER.debug(
                        "Skip sync task for %s because another task is already in progress",
                        provider_instance,
                    )
                    return

        provider = self.mass.get_provider(provider_instance)

        async def run_sync() -> None:
            # Wrap the provider sync into a lock to prevent
            # race conditions when multiple propviders are syncing at the same time.
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

        def on_sync_task_done(task: asyncio.Task):  # noqa: ARG001
            self.in_progress_syncs.remove(sync_spec)
            self.mass.signal_event(EventType.SYNC_TASKS_UPDATED, data=self.in_progress_syncs)
            # trigger metadata scan after provider sync completed
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
            prov_items = await ctrl.get_db_items_by_prov_id(provider_instance)
            for item in prov_items:
                await ctrl.remove_prov_mapping(item.item_id, provider_instance)

    async def _setup_database(self):
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

        if prev_version not in (0, SCHEMA_VERSION):
            LOGGER.info(
                "Performing database migration from %s to %s",
                prev_version,
                SCHEMA_VERSION,
            )

            if prev_version < 22:
                # for now just keep it simple and just recreate the tables if the schema is too old
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_ARTISTS}")
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_ALBUMS}")
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_TRACKS}")
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_PLAYLISTS}")
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_RADIOS}")

                # recreate missing tables
                await self.__create_database_tables()
            else:
                raise RuntimeError("db schema migration missing")

        # store current schema version
        await self.database.insert_or_replace(
            DB_TABLE_SETTINGS,
            {"key": "version", "value": str(SCHEMA_VERSION), "type": "str"},
        )
        # create indexes if needed
        await self.__create_database_indexes()
        # compact db
        await self.database.execute("VACUUM")

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
                timestamp INTEGER DEFAULT 0,
                UNIQUE(item_id, provider));"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUMS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    sort_artist TEXT,
                    album_type TEXT,
                    year INTEGER,
                    version TEXT,
                    in_library BOOLEAN DEFAULT 0,
                    barcode TEXT,
                    musicbrainz_id TEXT,
                    artists json,
                    metadata json,
                    provider_mappings json,
                    timestamp_added INTEGER NOT NULL,
                    timestamp_modified INTEGER NOT NULL
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ARTISTS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    musicbrainz_id TEXT,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_mappings json,
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
                    sort_album TEXT,
                    version TEXT,
                    duration INTEGER,
                    in_library BOOLEAN DEFAULT 0,
                    isrc TEXT,
                    musicbrainz_id TEXT,
                    artists json,
                    albums json,
                    metadata json,
                    provider_mappings json,
                    timestamp_added INTEGER NOT NULL,
                    timestamp_modified INTEGER NOT NULL
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_PLAYLISTS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    is_editable BOOLEAN NOT NULL,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_mappings json,
                    timestamp_added INTEGER NOT NULL,
                    timestamp_modified INTEGER NOT NULL
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_RADIOS}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    sort_name TEXT NOT NULL,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_mappings json,
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
            "CREATE INDEX IF NOT EXISTS artists_in_library_idx on artists(in_library);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS albums_in_library_idx on albums(in_library);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS tracks_in_library_idx on tracks(in_library);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS playlists_in_library_idx on playlists(in_library);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS radios_in_library_idx on radios(in_library);"
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
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS artists_musicbrainz_id_idx on artists(musicbrainz_id);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS albums_musicbrainz_id_idx on albums(musicbrainz_id);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS tracks_musicbrainz_id_idx on tracks(musicbrainz_id);"
        )
        await self.database.execute("CREATE INDEX IF NOT EXISTS tracks_isrc_idx on tracks(isrc);")
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS albums_barcode_idx on albums(barcode);"
        )
