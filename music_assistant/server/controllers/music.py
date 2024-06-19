"""MusicController: Orchestrates all data from music providers and sync to internal database."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from contextlib import suppress
from itertools import zip_longest
from math import inf
from typing import TYPE_CHECKING

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.global_cache import get_global_cache_value
from music_assistant.common.helpers.uri import parse_uri
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import (
    ConfigEntryType,
    EventType,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant.common.models.errors import (
    InvalidProviderID,
    InvalidProviderURI,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant.common.models.media_items import BrowseFolder, MediaItemType, SearchResults
from music_assistant.common.models.provider import SyncTask
from music_assistant.common.models.streamdetails import LoudnessMeasurement
from music_assistant.constants import (
    DB_SCHEMA_VERSION,
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_RADIOS,
    DB_TABLE_SETTINGS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACK_LOUDNESS,
    DB_TABLE_TRACKS,
    PROVIDERS_WITH_SHAREABLE_URLS,
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
CONF_DELETED_PROVIDERS = "deleted_providers"
CONF_ADD_LIBRARY_ON_PLAY = "add_library_on_play"


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
            ConfigEntry(
                key=CONF_ADD_LIBRARY_ON_PLAY,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
                label="Add item to the library as soon as its played",
                description="Automatically add a track or radio station to "
                "the library when played (if its not already in the library).",
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self.config = config
        # setup library database
        await self._setup_database()
        sync_interval = config.get_value(CONF_SYNC_INTERVAL)
        self.logger.info("Using a sync interval of %s minutes.", sync_interval)
        # make sure to finish any removal jobs
        for removed_provider in self.mass.config.get_raw_core_config_value(
            self.domain, CONF_DELETED_PROVIDERS, []
        ):
            await self.cleanup_provider(removed_provider)
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
        limit: int = 25,
    ) -> SearchResults:
        """Perform global search for media items on all providers.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: number of items to return in the search (per type).
        """
        if not media_types:
            media_types = MediaType.ALL
        # Check if the search query is a streaming provider public shareable URL
        try:
            media_type, provider_instance_id_or_domain, item_id = await parse_uri(
                search_query, validate_id=True
            )
        except InvalidProviderURI:
            pass
        except InvalidProviderID as err:
            self.logger.warning("%s", str(err))
            return SearchResults()
        else:
            if provider_instance_id_or_domain in PROVIDERS_WITH_SHAREABLE_URLS:
                try:
                    item = await self.get_item(
                        media_type=media_type,
                        item_id=item_id,
                        provider_instance_id_or_domain=provider_instance_id_or_domain,
                    )
                except MusicAssistantError as err:
                    self.logger.warning("%s", str(err))
                    return SearchResults()
                else:
                    if media_type == MediaType.ARTIST:
                        return SearchResults(artists=[item])
                    elif media_type == MediaType.ALBUM:
                        return SearchResults(albums=[item])
                    elif media_type == MediaType.TRACK:
                        return SearchResults(tracks=[item])
                    elif media_type == MediaType.PLAYLIST:
                        return SearchResults(playlists=[item])
                    else:
                        return SearchResults()

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
            ][:limit],
            albums=[
                item
                for sublist in zip_longest(*[x.albums for x in results_per_provider])
                for item in sublist
                if item is not None
            ][:limit],
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
            ][:limit],
            radio=[
                item
                for sublist in zip_longest(*[x.radio for x in results_per_provider])
                for item in sublist
                if item is not None
            ][:limit],
        )

    async def search_provider(
        self,
        search_query: str,
        provider_instance_id_or_domain: str,
        media_types: list[MediaType],
        limit: int = 10,
    ) -> SearchResults:
        """Perform search on given provider.

        :param search_query: Search query
        :param provider_instance_id_or_domain: instance_id or domain of the provider
                                               to perform the search on.
        :param provider_instance: instance id of the provider to perform the search on.
        :param media_types: A list of media_types to include.
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
    async def browse(self, offset: int, limit: int, path: str | None = None) -> list[MediaItemType]:
        """Browse Music providers."""
        if not path or path == "root":
            # root level; folder per provider
            root_items: list[MediaItemType] = []
            for prov in self.providers:
                if ProviderFeature.BROWSE not in prov.supported_features:
                    continue
                root_items.append(
                    BrowseFolder(
                        item_id="root",
                        provider=prov.domain,
                        path=f"{prov.instance_id}://",
                        uri=f"{prov.instance_id}://",
                        name=prov.name,
                    )
                )
            return root_items

        # provider level
        prepend_items: list[MediaItemType] = []
        provider_instance, sub_path = path.split("://", 1)
        prov = self.mass.get_provider(provider_instance)
        # handle regular provider listing, always add back folder first
        if not prov or not sub_path:
            prepend_items.append(
                BrowseFolder(item_id="root", provider="library", path="root", name="..")
            )
            if not prov:
                return prepend_items
        elif offset == 0:
            back_path = f"{provider_instance}://" + "/".join(sub_path.split("/")[:-1])
            prepend_items.append(
                BrowseFolder(item_id="back", provider=provider_instance, path=back_path, name="..")
            )
        # limit -1 to account for the prepended items
        prov_items = await prov.browse(path=path, offset=offset, limit=limit)
        return prepend_items + prov_items

    @api_command("music/recently_played_items")
    async def recently_played(
        self, limit: int = 10, media_types: list[MediaType] | None = None
    ) -> list[MediaItemType]:
        """Return a list of the last played items."""
        if media_types is None:
            media_types = MediaType.ALL
        media_types_str = "(" + ",".join(f'"{x}"' for x in media_types) + ")"
        query = (
            f"SELECT * FROM {DB_TABLE_PLAYLOG} WHERE media_type "
            f"in {media_types_str} ORDER BY timestamp DESC"
        )
        db_rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)
        result: list[MediaItemType] = []
        for db_row in db_rows:
            if db_row["provider"] not in get_global_cache_value("unique_providers", []):
                continue
            with suppress(MediaNotFoundError, ProviderUnavailableError):
                media_type = MediaType(db_row["media_type"])
                ctrl = self.get_controller(media_type)
                item = await ctrl.get(
                    db_row["item_id"],
                    db_row["provider"],
                    add_to_library=False,
                    lazy=True,
                    force_refresh=False,
                )
                result.append(item)
        return result

    @api_command("music/item_by_uri")
    async def get_item_by_uri(
        self, uri: str, lazy: bool = True, add_to_library: bool = False
    ) -> MediaItemType:
        """Fetch MediaItem by uri."""
        media_type, provider_instance_id_or_domain, item_id = await parse_uri(uri)
        return await self.get_item(
            media_type=media_type,
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            lazy=lazy,
            add_to_library=add_to_library,
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
        if provider_instance_id_or_domain == "builtin":
            # handle special case of 'builtin' MusicProvider which allows us to play regular url's
            return await self.mass.get_provider("builtin").parse_item(item_id)
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
        # ensure item is added to streaming provider library
        if (
            (provider := self.mass.get_provider(item.provider))
            and provider.is_streaming_provider
            and provider.library_edit_supported(item.media_type)
        ):
            await provider.library_add(item)
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
            if prov_controller := self.mass.get_provider(provider_mapping.provider_instance):
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
            await provider.library_add(item)
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
        self, item_id: str, provider_instance_id_or_domain: str, loudness: LoudnessMeasurement
    ) -> None:
        """Store Loudness Measurement for a track in db."""
        if provider := self.mass.get_provider(provider_instance_id_or_domain):
            await self.database.insert(
                DB_TABLE_TRACK_LOUDNESS,
                {
                    "item_id": item_id,
                    "provider": provider.lookup_key,
                    "integrated": round(loudness.integrated, 2),
                    "true_peak": round(loudness.true_peak, 2),
                    "lra": round(loudness.lra, 2),
                    "threshold": round(loudness.threshold, 2),
                    "target_offset": round(loudness.target_offset, 2),
                },
                allow_replace=True,
            )

    async def get_track_loudness(
        self, item_id: str, provider_instance_id_or_domain: str
    ) -> LoudnessMeasurement | None:
        """Get Loudness Measurement for a track in db."""
        if provider := self.mass.get_provider(provider_instance_id_or_domain):
            if result := await self.database.get_row(
                DB_TABLE_TRACK_LOUDNESS,
                {
                    "item_id": item_id,
                    "provider": provider.lookup_key,
                },
            ):
                if result["integrated"] == inf or result["integrated"] == -inf:
                    return None

                return LoudnessMeasurement(
                    integrated=result["integrated"],
                    true_peak=result["true_peak"],
                    lra=result["lra"],
                    threshold=result["threshold"],
                    target_offset=result["target_offset"],
                )
        return None

    async def mark_item_played(
        self, media_type: MediaType, item_id: str, provider_instance_id_or_domain: str
    ) -> None:
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()

        if provider_instance_id_or_domain == "builtin":
            # we deliberately skip builtin provider items as those are often
            # one-off items like TTS or some sound effect etc.
            return

        if provider_instance_id_or_domain == "library":
            prov_key = "library"
        elif prov := self.mass.get_provider(provider_instance_id_or_domain):
            prov_key = prov.lookup_key
        else:
            prov_key = provider_instance_id_or_domain

        # update generic playlog table
        await self.database.insert(
            DB_TABLE_PLAYLOG,
            {
                "item_id": item_id,
                "provider": prov_key,
                "media_type": media_type.value,
                "timestamp": timestamp,
            },
            allow_replace=True,
        )

        # also update playcount in library table
        ctrl = self.get_controller(media_type)
        if self.mass.config.get_raw_core_config_value(self.domain, CONF_ADD_LIBRARY_ON_PLAY):
            # handle feature to add to the lib on playback
            db_item = await ctrl.get(
                item_id, provider_instance_id_or_domain, lazy=False, add_to_library=True
            )
        else:
            db_item = await ctrl.get_library_item_by_prov_id(
                item_id, provider_instance_id_or_domain
            )
        if db_item:
            await self.database.execute(
                f"UPDATE {ctrl.db_table} SET play_count = play_count + 1, "
                f"last_played = {timestamp} WHERE item_id = {db_item.item_id}"
            )
        await self.database.commit()

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
            # schedule db cleanup after sync
            if not self.in_progress_syncs:
                self.mass.create_task(self._cleanup_database())

        task.add_done_callback(on_sync_task_done)

    async def cleanup_provider(self, provider_instance: str) -> None:
        """Cleanup provider records from the database."""
        deleted_providers = self.mass.config.get_raw_core_config_value(
            self.domain, CONF_DELETED_PROVIDERS, []
        )
        # we add the provider to this hidden config setting just to make sure that
        # we can survive this over a restart to make sure that entries are cleaned up
        if provider_instance not in deleted_providers:
            deleted_providers.append(provider_instance)
            self.mass.config.set_raw_core_config_value(
                self.domain, CONF_DELETED_PROVIDERS, deleted_providers
            )
            self.mass.config.save(True)

        # clean cache items from deleted provider(s)
        await self.mass.cache.clear(provider_instance)

        # cleanup media items from db matched to deleted provider
        self.logger.info(
            "Removing provider %s from library, this can take a a while...", provider_instance
        )
        errors = 0
        for ctrl in (
            # order is important here to recursively cleanup bottom up
            self.mass.music.radio,
            self.mass.music.playlists,
            self.mass.music.tracks,
            self.mass.music.albums,
            self.mass.music.artists,
            # run main controllers twice to rule out relations
            self.mass.music.tracks,
            self.mass.music.albums,
            self.mass.music.artists,
        ):
            query = (
                f"SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                f"WHERE media_type = '{ctrl.media_type}' "
                f"AND provider_instance = '{provider_instance}'"
            )
            for db_row in await self.database.get_rows_from_query(query, limit=100000):
                try:
                    await ctrl.remove_provider_mappings(db_row["item_id"], provider_instance)
                except Exception as err:
                    # we dont want the whole removal process to stall on one item
                    # so in case of an unexpected error, we log and move on.
                    self.logger.warning(
                        "Error while removing %s: %s",
                        db_row["item_id"],
                        str(err),
                        exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
                    )
                    errors += 1

        # remove all orphaned items (not in provider mappings table anymore)
        query = (
            f"SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
            f"WHERE provider_instance = '{provider_instance}'"
        )
        if remaining_items_count := await self.database.get_count_from_query(query):
            errors += remaining_items_count

        # cleanup playlog table
        await self.mass.music.database.delete(
            DB_TABLE_PLAYLOG,
            {
                "provider": provider_instance,
            },
        )

        if errors == 0:
            # cleanup successful, remove from the deleted_providers setting
            self.logger.info("Provider %s removed from library", provider_instance)
            deleted_providers.remove(provider_instance)
            self.mass.config.set_raw_core_config_value(
                self.domain, CONF_DELETED_PROVIDERS, deleted_providers
            )
        else:
            self.logger.warning(
                "Provider %s was not not fully removed from library", provider_instance
            )

    def _schedule_sync(self) -> None:
        """Schedule the periodic sync."""
        self.start_sync()
        sync_interval = self.config.get_value(CONF_SYNC_INTERVAL)
        # we reschedule ourselves right after execution
        # NOTE: sync_interval is stored in minutes, we need seconds
        self.mass.loop.call_later(sync_interval * 60, self._schedule_sync)

    async def _cleanup_database(self) -> None:
        """Perform database cleanup/maintenance."""
        self.logger.debug("Performing database cleanup...")
        # Remove playlog entries older than 90 days
        await self.database.delete_where_query(
            DB_TABLE_PLAYLOG, f"timestamp < strftime('%s','now') - {3600 * 24  * 90}"
        )
        # db tables cleanup
        for ctrl in (self.albums, self.artists, self.tracks, self.playlists, self.radio):
            # Provider mappings where the db item is removed
            query = (
                f"item_id not in (SELECT item_id from {ctrl.db_table}) "
                f"AND media_type = '{ctrl.media_type}'"
            )
            await self.database.delete_where_query(DB_TABLE_PROVIDER_MAPPINGS, query)
            # Orphaned db items
            query = (
                f"item_id not in (SELECT item_id from {DB_TABLE_PROVIDER_MAPPINGS} "
                f"WHERE media_type = '{ctrl.media_type}')"
            )
            await self.database.delete_where_query(ctrl.db_table, query)
            # Cleanup removed db items from the playlog
            where_clause = (
                f"media_type = '{ctrl.media_type}' AND provider = 'library' "
                f"AND item_id not in (select item_id from {ctrl.db_table})"
            )
            await self.mass.music.database.delete_where_query(DB_TABLE_PLAYLOG, where_clause)
        self.logger.debug("Database cleanup done")

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

            # handle db migration from previous schema(s) to this one
            try:
                await self.__migrate_database(prev_version)
            except Exception as err:
                self.logger.fatal(
                    "Database migration failed - setup can not continue. "
                    "Try restarting the server. If this issue persists, create an issue report "
                    " on Github and/or re-install the server (or restore a backup).",
                    exc_info=err,
                )
                # restore backup file
                await asyncio.to_thread(shutil.copyfile, db_path_backup, db_path)
                raise RuntimeError("Database migration failed") from err

        # store current schema version
        await self.database.insert_or_replace(
            DB_TABLE_SETTINGS,
            {"key": "version", "value": str(DB_SCHEMA_VERSION), "type": "str"},
        )
        # create indexes and triggers if needed
        await self.__create_database_indexes()
        await self.__create_database_triggers()
        # compact db
        self.logger.debug("Compacting database...")
        try:
            await self.database.vacuum()
        except Exception as err:
            self.logger.warning("Database vacuum failed: %s", str(err))
        else:
            self.logger.debug("Compacting database done")

    async def __migrate_database(self, prev_version: int) -> None:
        """Perform a database migration."""
        self.logger.info(
            "Migrating database from version %s to %s", prev_version, DB_SCHEMA_VERSION
        )
        if prev_version == 1:
            # migrate from version 1 to 2
            await self.database.execute(
                f"DELETE FROM {DB_TABLE_PLAYLOG} WHERE provider = 'builtin'"
            )
            await self.database.commit()
            return

        # all other versions: reset the database
        # we only migrate from prtev version to current we do not try to handle
        # more complex migrations
        self.logger.warning(
            "Database schema too old - Resetting library/database - "
            "a full rescan will be performed, this can take a while!"
        )
        for table in (
            DB_TABLE_TRACKS,
            DB_TABLE_ALBUMS,
            DB_TABLE_ARTISTS,
            DB_TABLE_PLAYLISTS,
            DB_TABLE_RADIOS,
            DB_TABLE_ALBUM_TRACKS,
            DB_TABLE_PLAYLOG,
            DB_TABLE_TRACK_LOUDNESS,
            DB_TABLE_PROVIDER_MAPPINGS,
        ):
            await self.database.execute(f"DROP TABLE IF EXISTS {table}")
        await self.database.commit()
        # recreate missing tables
        await self.__create_database_tables()

    async def __create_database_tables(self) -> None:
        """Create database tables."""
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_SETTINGS}(
                    [key] TEXT PRIMARY KEY,
                    [value] TEXT,
                    [type] TEXT
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_TRACK_LOUDNESS}(
                    [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [item_id] TEXT NOT NULL,
                    [provider] TEXT NOT NULL,
                    [integrated] REAL,
                    [true_peak] REAL,
                    [lra] REAL,
                    [threshold] REAL,
                    [target_offset] REAL,
                    UNIQUE(item_id, provider));"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_PLAYLOG}(
                [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                [item_id] TEXT NOT NULL,
                [provider] TEXT NOT NULL,
                [media_type] TEXT NOT NULL DEFAULT 'track',
                [timestamp] INTEGER DEFAULT 0,
                UNIQUE(item_id, provider, media_type));"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUMS}(
                    [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [name] TEXT NOT NULL,
                    [sort_name] TEXT NOT NULL,
                    [version] TEXT,
                    [album_type] TEXT NOT NULL,
                    [year] INTEGER,
                    [favorite] BOOLEAN DEFAULT 0,
                    [metadata] json NOT NULL,
                    [external_ids] json NOT NULL,
                    [play_count] INTEGER DEFAULT 0,
                    [last_played] INTEGER DEFAULT 0,
                    [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                    [timestamp_modified] INTEGER,

                    [artists] json DEFAULT '[]',
                    [provider_mappings] json DEFAULT '[]'
                );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_ARTISTS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [favorite] BOOLEAN DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER,

            [provider_mappings] json DEFAULT '[]'
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_TRACKS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [version] TEXT,
            [duration] INTEGER,
            [favorite] BOOLEAN DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER,

            [artists] json DEFAULT '[]',
            [provider_mappings] json DEFAULT '[]'
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_PLAYLISTS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [owner] TEXT NOT NULL,
            [is_editable] BOOLEAN NOT NULL,
            [favorite] BOOLEAN DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER,

            [provider_mappings] json DEFAULT '[]'
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_RADIOS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [favorite] BOOLEAN DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER,

            [provider_mappings] json DEFAULT '[]'
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUM_TRACKS}(
            [id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [track_id] INTEGER NOT NULL,
            [album_id] INTEGER NOT NULL,
            [disc_number] INTEGER NOT NULL,
            [track_number] INTEGER NOT NULL,
            FOREIGN KEY([track_id]) REFERENCES [tracks]([item_id]),
            FOREIGN KEY([album_id]) REFERENCES [albums]([item_id]),
            UNIQUE(track_id, album_id)
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_PROVIDER_MAPPINGS}(
            [media_type] TEXT NOT NULL,
            [item_id] INTEGER NOT NULL,
            [provider_domain] TEXT NOT NULL,
            [provider_instance] TEXT NOT NULL,
            [provider_item_id] TEXT NOT NULL,
            [available] BOOLEAN DEFAULT 1,
            [url] text,
            [audio_format] json,
            [details] TEXT,
            UNIQUE(media_type, provider_instance, provider_item_id)
            );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_TRACK_ARTISTS}(
            [track_id] INTEGER NOT NULL,
            [artist_id] INTEGER NOT NULL,
            FOREIGN KEY([track_id]) REFERENCES [tracks]([item_id]),
            FOREIGN KEY([artist_id]) REFERENCES [artists]([item_id]),
            UNIQUE(track_id, artist_id)
            );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUM_ARTISTS}(
            [album_id] INTEGER NOT NULL,
            [artist_id] INTEGER NOT NULL,
            FOREIGN KEY([album_id]) REFERENCES [albums]([item_id]),
            FOREIGN KEY([artist_id]) REFERENCES [artists]([item_id]),
            UNIQUE(album_id, artist_id)
            );"""
        )
        await self.database.commit()

    async def __create_database_indexes(self) -> None:
        """Create database indexes."""
        for db_table in (
            DB_TABLE_ARTISTS,
            DB_TABLE_ALBUMS,
            DB_TABLE_TRACKS,
            DB_TABLE_PLAYLISTS,
            DB_TABLE_RADIOS,
        ):
            # index on favorite column
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_favorite_idx on {db_table}(favorite);"
            )
            # index on name
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_name_idx on {db_table}(name);"
            )
            # index on name (without case sensitivity)
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_name_nocase_idx "
                f"ON {db_table}(name COLLATE NOCASE);"
            )
            # index on sort_name
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_sort_name_idx on {db_table}(sort_name);"
            )
            # index on sort_name (without case sensitivity)
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_sort_name_nocase_idx "
                f"ON {db_table}(sort_name COLLATE NOCASE);"
            )
            # index on external_ids
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_external_ids_idx "
                f"ON {db_table}(external_ids);"
            )
            # index on timestamp_added
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_timestamp_added_idx "
                f"on {db_table}(timestamp_added);"
            )
            # index on play_count
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_play_count_idx "
                f"on {db_table}(play_count);"
            )
            # index on last_played
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_last_played_idx "
                f"on {db_table}(last_played);"
            )

        # indexes on provider_mappings table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_PROVIDER_MAPPINGS}_media_type_item_id_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,item_id);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_PROVIDER_MAPPINGS}_provider_domain_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_domain,provider_item_id);"
        )
        await self.database.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {DB_TABLE_PROVIDER_MAPPINGS}_provider_instance_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_instance,provider_item_id);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS "
            f"{DB_TABLE_PROVIDER_MAPPINGS}_media_type_provider_instance_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_instance);"
        )
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS "
            f"{DB_TABLE_PROVIDER_MAPPINGS}_media_type_provider_domain_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_domain);"
        )

        # indexes on track_artists table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_TRACK_ARTISTS}_track_id_idx "
            f"on {DB_TABLE_TRACK_ARTISTS}(track_id);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_TRACK_ARTISTS}_artist_id_idx "
            f"on {DB_TABLE_TRACK_ARTISTS}(artist_id);"
        )
        # indexes on album_artists table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_ALBUM_ARTISTS}_album_id_idx "
            f"on {DB_TABLE_ALBUM_ARTISTS}(album_id);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_ALBUM_ARTISTS}_artist_id_idx "
            f"on {DB_TABLE_ALBUM_ARTISTS}(artist_id);"
        )
        await self.database.commit()

    async def __create_database_triggers(self) -> None:
        """Create database triggers."""
        # triggers to auto update timestamps
        for db_table in ("artists", "albums", "tracks", "playlists", "radios"):
            await self.database.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS update_{db_table}_timestamp
                AFTER UPDATE ON {db_table} FOR EACH ROW
                WHEN NEW.timestamp_modified <= OLD.timestamp_modified
                BEGIN
                    UPDATE {db_table} set timestamp_modified=cast(strftime('%s','now') as int)
                    WHERE item_id=OLD.item_id;
                END;
                """
            )
        await self.database.commit()
