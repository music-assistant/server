"""MusicController: Orchestrates all data from music providers and sync to internal database."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import itertools
import logging
import os
import statistics
from typing import TYPE_CHECKING

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.uri import parse_uri
from music_assistant.common.models.config_entries import (
    CONF_KEY_ENABLED,
    CONFIG_ENTRY_ENABLED,
    ProviderConfig,
)
from music_assistant.common.models.enums import (
    EventType,
    MediaType,
    MusicProviderFeature,
    ProviderType,
)
from music_assistant.common.models.errors import (
    MusicAssistantError,
    ProviderUnavailableError,
    SetupFailedError,
)
from music_assistant.common.models.event import MassEvent
from music_assistant.common.models.media_items import (
    BrowseFolder,
    MediaItem,
    MediaItemType,
    media_from_dict,
)
from music_assistant.common.models.provider_manifest import ProviderManifest
from music_assistant.constants import (
    CONF_DB_LIBRARY,
    DEFAULT_DB_LIBRARY,
    ROOT_LOGGER_NAME,
    DB_TABLE_SETTINGS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_RADIOS,
    DB_TABLE_TRACK_LOUDNESS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_TRACKS,
)
from music_assistant.server.helpers.api import api_command
from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.server.helpers.database import DatabaseConnection

from .media.albums import AlbumsController
from .media.artists import ArtistsController
from .media.playlists import PlaylistController
from .media.radio import RadioController
from .media.tracks import TracksController

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.music")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(BASE_DIR)
PROVIDERS_PATH = os.path.join(SERVER_DIR, "music_providers")

SCHEMA_VERSION = 19


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
        self._available_providers: dict[str, ProviderManifest] = {}
        self._providers: dict[str, MusicProvider] = {}

    async def setup(self):
        """Async initialize of module."""
        # setup library database
        await self._setup_database()
        # load available providers
        await self._load_available_providers()
        # load providers from config
        for prov_conf in self.mass.config.get_provider_configs(ProviderType.MUSIC):
            await self._load_provider(prov_conf)
        # check for any 'load_by_default' providers (e.g. URL provider)
        for prov_manifest in self._available_providers.values():
            if not prov_manifest.load_by_default:
                continue
            loaded = any(
                (x for x in self.providers if x.domain == prov_manifest.domain)
            )
            if loaded:
                continue
            await self._load_provider(
                ProviderConfig(
                    type=prov_manifest.type,
                    domain=prov_manifest.domain,
                    instance_id=prov_manifest.domain,
                    values={},
                )
            )
        # listen to config change events
        self.mass.subscribe(
            self._handle_config_updated_event,
            (EventType.PROVIDER_CONFIG_CREATED, EventType.PROVIDER_CONFIG_UPDATED),
        )
        # add task to cleanup old records from db
        self.mass.create_task(self._cleanup_library())

    @api_command("music/providers/available")
    def get_available_providers(self) -> list[ProviderManifest]:
        """Return all available MusicProviders."""
        return list(self._available_providers.values())

    @property
    def providers(self) -> list[MusicProvider]:
        """Return all loaded/running MusicProviders (instances)."""
        return list(self._providers.values())

    def get_provider(self, provider_instance_or_domain: str) -> MusicProvider:
        """Return Music provider by instance id (or domain)."""
        if prov := self._providers.get(provider_instance_or_domain):
            return prov
        for prov in self._providers.values():
            if provider_instance_or_domain in (prov.type, prov.id, prov.type):
                return prov
        raise ProviderUnavailableError(
            f"Provider {provider_instance_or_domain} is not available"
        )

    @api_command("music/sync")
    async def start_sync(
        self,
        media_types: tuple[MediaType] | None = None,
        providers: tuple[str] | None = None,
    ) -> None:
        """
        Start running the sync of (all or selected) musicproviders.

        media_types: only sync these media types. None for all.
        providers: only sync these provider domains. None for all.
        """

        for provider in self.providers:
            if providers is not None and provider.domain not in providers:
                continue
            # TODO: Add task to list to track progress
            self.mass.create_task(
                provider.sync_library(media_types),
            )

    async def search(
        self,
        search_query,
        media_types: list[MediaType] = MediaType.ALL,
        limit: int = 10,
    ) -> list[MediaItemType]:
        """
        Perform global search for media items on all providers.

            :param search_query: Search query.
            :param media_types: A list of media_types to include.
            :param limit: number of items to return in the search (per type).
        """
        # include results from all music providers
        provider_instances = (item.id for item in self.providers)
        # TODO: sort by name and filter out duplicates ?
        return itertools.chain.from_iterable(
            await asyncio.gather(
                *[
                    self.search_provider(
                        search_query,
                        media_types,
                        provider_instance=provider_instance,
                        limit=limit,
                    )
                    for provider_instance in provider_instances
                ]
            )
        )

    async def search_provider(
        self,
        search_query: str,
        media_types: list[MediaType] = MediaType.ALL,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        limit: int = 10,
    ) -> list[MediaItemType]:
        """
        Perform search on given provider.

            :param search_query: Search query
            :param provider_domain: domain of the provider to perform the search on.
            :param provider_instance: instance id of the provider to perform the search on.
            :param media_types: A list of media_types to include. All types if None.
            :param limit: number of items to return in the search (per type).
        """
        assert provider_domain or provider_instance, "Provider needs to be supplied"
        prov = self.get_provider(provider_instance or provider_domain)
        if MusicProviderFeature.SEARCH not in prov.supported_features:
            return []

        # create safe search string
        search_query = search_query.replace("/", " ").replace("'", "")

        # prefer cache items (if any)
        cache_key = f"{prov.type}.search.{search_query}.{limit}"
        cache_key += "".join((x for x in media_types))

        if cache := await self.mass.cache.get(cache_key):
            return [media_from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        items = await prov.search(
            search_query,
            media_types,
            limit,
        )
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(
                cache_key, [x.to_dict() for x in items], expiration=86400 * 7
            )
        )
        return items

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
                        provider=prov.type,
                        path=f"{prov.id}://",
                        name=prov.name,
                    )
                    for prov in self.providers
                    if MusicProviderFeature.BROWSE in prov.supported_features
                ],
            )
        # provider level
        provider_instance = path.split("://", 1)[0]
        prov = self.get_provider(provider_instance)
        return await prov.browse(path)

    async def get_item_by_uri(
        self, uri: str, force_refresh: bool = False, lazy: bool = True
    ) -> MediaItemType:
        """Fetch MediaItem by uri."""
        media_type, provider_domain, item_id = parse_uri(uri)
        return await self.get_item(
            item_id=item_id,
            media_type=media_type,
            provider_domain=provider_domain,
            force_refresh=force_refresh,
            lazy=lazy,
        )

    async def get_item(
        self,
        item_id: str,
        media_type: MediaType,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        force_refresh: bool = False,
        lazy: bool = True,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        assert (
            provider_domain or provider_instance
        ), "provider_domain or provider_instance must be supplied"
        if "url" in (provider_domain, provider_instance):
            # handle special case of 'URL' MusicProvider which allows us to play regular url's
            return await self.get_provider("url").parse_item(item_id)
        ctrl = self.get_controller(media_type)
        return await ctrl.get(
            provider_item_id=item_id,
            provider_domain=provider_domain,
            provider_instance=provider_instance,
            force_refresh=force_refresh,
            lazy=lazy,
        )

    async def add_to_library(
        self,
        media_type: MediaType,
        provider_item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
    ) -> None:
        """Add an item to the library."""
        ctrl = self.get_controller(media_type)
        await ctrl.add_to_library(
            provider_item_id,
            provider_domain=provider_domain,
            provider_instance=provider_instance,
        )

    async def remove_from_library(
        self,
        media_type: MediaType,
        provider_item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
    ) -> None:
        """Remove item from the library."""
        ctrl = self.get_controller(media_type)
        await ctrl.remove_from_library(
            provider_item_id,
            provider_domain=provider_domain,
            provider_instance=provider_instance,
        )

    async def delete_db_item(
        self, media_type: MediaType, db_item_id: str, recursive: bool = False
    ) -> None:
        """Remove item from the library."""
        ctrl = self.get_controller(media_type)
        await ctrl.delete_db_item(db_item_id, recursive)

    async def refresh_items(self, items: list[MediaItem]) -> None:
        """
        Refresh MediaItems to force retrieval of full info and matches.

        Creates background tasks to process the action.
        """
        for media_item in items:
            self.mass.create_task(self.refresh_item(media_item))

    async def refresh_item(
        self,
        media_item: MediaItem,
    ):
        """Try to refresh a mediaitem by requesting it's full object or search for substitutes."""
        try:
            return await self.get_item(
                media_item.item_id,
                media_item.media_type,
                provider_domain=media_item.provider,
                force_refresh=True,
                lazy=False,
            )
        except MusicAssistantError:
            pass

        for item in await self.search(media_item.name, [media_item.media_type], 20):
            if item.available:
                await self.get_item(
                    item.item_id, item.media_type, item.provider, lazy=False
                )

    async def set_track_loudness(
        self, item_id: str, provider_domain: str, loudness: int
    ):
        """list integrated loudness for a track in db."""
        await self.database.insert(
            DB_TABLE_TRACK_LOUDNESS,
            {"item_id": item_id, "provider": provider_domain, "loudness": loudness},
            allow_replace=True,
        )

    async def get_track_loudness(
        self, provider_item_id: str, provider_domain: str
    ) -> float | None:
        """Get integrated loudness for a track in db."""
        if result := await self.database.get_row(
            DB_TABLE_TRACK_LOUDNESS,
            {
                "item_id": provider_item_id,
                "provider": provider_domain,
            },
        ):
            return result["loudness"]
        return None

    async def get_provider_loudness(self, provider_domain: str) -> float | None:
        """Get average integrated loudness for tracks of given provider."""
        all_items = []
        if provider_domain == "url":
            # this is not a very good idea for random urls
            return None
        for db_row in await self.database.get_rows(
            DB_TABLE_TRACK_LOUDNESS,
            {
                "provider": provider_domain,
            },
        ):
            all_items.append(db_row["loudness"])
        if all_items:
            return statistics.fmean(all_items)
        return None

    async def mark_item_played(self, item_id: str, provider_domain: str):
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()
        await self.database.insert(
            DB_TABLE_PLAYLOG,
            {
                "item_id": item_id,
                "provider": provider_domain,
                "timestamp": timestamp,
            },
            allow_replace=True,
        )

    async def library_add_items(self, items: list[MediaItem]) -> None:
        """
        Add media item(s) to the library.

        Creates background tasks to process the action.
        """
        for media_item in items:
            self.mass.create_task(
                self.add_to_library(
                    media_item.media_type, media_item.item_id, media_item.provider
                )
            )

    async def library_remove_items(self, items: list[MediaItem]) -> None:
        """
        Remove media item(s) from the library.

        Creates background tasks to process the action.
        """
        for media_item in items:
            self.mass.create_task(
                self.remove_from_library(
                    media_item.media_type, media_item.item_id, media_item.provider
                )
            )

    def get_controller(
        self, media_type: MediaType
    ) -> ArtistsController | AlbumsController | TracksController | RadioController | PlaylistController:
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

    async def _load_provider(self, conf: ProviderConfig) -> None:
        """Load (or reload) a music provider."""
        assert conf.type == ProviderType.MUSIC
        if provider := self._providers.get(conf.instance_id):
            # provider is already loaded, stop and unload it first
            await provider.close()
            self._providers.pop(conf.instance_id)

        domain = conf.domain
        prov_manifest = self._available_providers.get(domain)
        # check for other instances of this provider
        existing = next((x for x in self.providers if x.domain == domain), None)
        if existing and not prov_manifest.multi_instance:
            raise SetupFailedError(
                f"Provider {domain} already loaded and only one instance allowed."
            )

        if not prov_manifest:
            raise SetupFailedError(f"Provider {domain} manifest not found")
        # try to load the module
        try:
            prov_mod = importlib.import_module(
                f".{domain}", "music_assistant.server.music_providers"
            )
            for name, obj in inspect.getmembers(prov_mod):
                if not inspect.isclass(obj):
                    continue
                # lookup class to initialize
                if name == prov_manifest.init_class or (
                    not prov_manifest.init_class
                    and issubclass(obj, MusicProvider)
                    and obj != MusicProvider
                ):
                    prov_cls = obj
                    break
            else:
                SetupFailedError("Unable to locate Provider class")
            provider: MusicProvider = prov_cls(self.mass, prov_manifest, conf)
            self._providers[provider.instance_id] = provider
            await provider.setup()
        # pylint: disable=broad-except
        except Exception as exc:
            LOGGER.exception(
                "Error loading provider(instance) %s: %s",
                conf.title or conf.domain,
                str(exc),
            )
        else:
            LOGGER.debug(
                "Successfully loaded provider %s",
                conf.title or conf.domain,
            )

    async def _cleanup_library(self) -> None:
        """Cleanup deleted items from library/database."""
        prev_providers = await self.mass.cache.get("prov_ids", default=[])
        cur_providers = list(self._providers.keys())
        removed_providers = {x for x in prev_providers if x not in cur_providers}

        for provider_instance in removed_providers:

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
                prov_items = await ctrl.get_db_items_by_prov_id(
                    provider_instance=provider_instance
                )
                for item in prov_items:
                    await ctrl.remove_prov_mapping(item.item_id, provider_instance)
        await self.mass.cache.set("prov_ids", cur_providers)

    async def _load_available_providers(self) -> None:
        """Preload all available provider manifest files."""
        for dir_str in os.listdir(PROVIDERS_PATH):
            dir_path = os.path.join(PROVIDERS_PATH, dir_str)
            if not os.path.isdir(dir_path):
                continue
            # get files in subdirectory
            for file_str in os.listdir(dir_path):
                file_path = os.path.join(dir_path, file_str)
                if not os.path.isfile(file_path):
                    continue
                if file_str != "manifest.json":
                    continue
                try:
                    provider_manifest = await ProviderManifest.parse(file_path)
                    # inject config entry to enable/disable the provider
                    conf_keys = (x.key for x in provider_manifest.config_entries)
                    if CONF_KEY_ENABLED not in conf_keys:
                        provider_manifest.config_entries = [
                            CONFIG_ENTRY_ENABLED,
                            *provider_manifest.config_entries,
                        ]
                    self._available_providers[
                        provider_manifest.domain
                    ] = provider_manifest
                    LOGGER.debug("Loaded manifest for MusicProvider %s", dir_str)
                except Exception as exc:  # pylint: disable=broad-except
                    LOGGER.exception(
                        "Error while loading manifest for provider %s",
                        dir_str,
                        exc_info=exc,
                    )

    async def _handle_config_updated_event(self, event: MassEvent):
        """Handle ProviderConfig updated/created event."""
        if event.data.type != ProviderType.MUSIC:
            return
        await self._load_provider(event.data)

    async def _setup_database(self):
        """Initialize database."""
        db_url: str = self.mass.config.get(CONF_DB_LIBRARY, DEFAULT_DB_LIBRARY)
        db_url = db_url.replace("[storage_path]", self.mass.storage_path)
        self.database = DatabaseConnection(db_url)

        # always create db tables if they don't exist to prevent errors trying to access them later
        await self.__create_database_tables()
        try:
            if db_row := await self.database.get_row(
                DB_TABLE_SETTINGS, {"key": "version"}
            ):
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

            if prev_version < SCHEMA_VERSION:
                # for now just keep it simple and just recreate the tables
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_ARTISTS}")
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_ALBUMS}")
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_TRACKS}")
                await self.database.execute(
                    f"DROP TABLE IF EXISTS {DB_TABLE_PLAYLISTS}"
                )
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_RADIOS}")
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_CACHE}")
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_THUMBS}")
                # recreate missing tables
                await self.__create_database_tables()

        # store current schema version
        await self.database.insert_or_replace(
            DB_TABLE_SETTINGS,
            {"key": "version", "value": str(SCHEMA_VERSION), "type": "str"},
        )
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
                    upc TEXT,
                    musicbrainz_id TEXT,
                    artists json,
                    metadata json,
                    provider_mappings json,
                    timestamp INTEGER DEFAULT 0
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
                    timestamp INTEGER DEFAULT 0
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
                    timestamp INTEGER DEFAULT 0
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
                    timestamp INTEGER DEFAULT 0,
                    UNIQUE(name, owner)
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
                    timestamp INTEGER DEFAULT 0
                );"""
        )

        # create indexes
        # TODO: create indexes for the json columns ?
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
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS tracks_isrc_idx on tracks(isrc);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS albums_upc_idx on albums(upc);"
        )
