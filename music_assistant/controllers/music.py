"""MusicController: Orchestrates all data from music providers and sync to internal database."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime
from itertools import zip_longest
from math import inf
from typing import TYPE_CHECKING, Final, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import (
    InvalidProviderID,
    InvalidProviderURI,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import (
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    MediaItemTypeOrItemMapping,
    RecommendationFolder,
    SearchResults,
)
from music_assistant_models.provider import SyncTask
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    CACHE_CATEGORY_MUSIC_SEARCH,
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_LOUDNESS_MEASUREMENTS,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PODCASTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_RADIOS,
    DB_TABLE_SETTINGS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
    PROVIDERS_WITH_SHAREABLE_URLS,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.compare import create_safe_string
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.json import json_loads, serialize_to_json
from music_assistant.helpers.uri import parse_uri
from music_assistant.helpers.util import TaskManager
from music_assistant.models.core_controller import CoreController

from .media.albums import AlbumsController
from .media.artists import ArtistsController
from .media.audiobooks import AudiobooksController
from .media.playlists import PlaylistController
from .media.podcasts import PodcastsController
from .media.radio import RadioController
from .media.tracks import TracksController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.media_items import Audiobook, PodcastEpisode

    from music_assistant.models.music_provider import MusicProvider

CONF_RESET_DB = "reset_db"
DEFAULT_SYNC_INTERVAL = 12 * 60  # default sync interval in minutes
CONF_SYNC_INTERVAL = "sync_interval"
CONF_DELETED_PROVIDERS = "deleted_providers"
CONF_ADD_LIBRARY_ON_PLAY = "add_library_on_play"
DB_SCHEMA_VERSION: Final[int] = 17


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
        self.audiobooks = AudiobooksController(self.mass)
        self.podcasts = PodcastsController(self.mass)
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
        entries = (
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
            ConfigEntry(
                key=CONF_RESET_DB,
                type=ConfigEntryType.ACTION,
                label="Reset library database",
                description="This will issue a full reset of the library "
                "database and trigger a full sync. Only use this option as a last resort "
                "if you are seeing issues with the library database.",
                category="advanced",
            ),
        )
        if action == CONF_RESET_DB:
            await self._reset_database()
            await self.mass.cache.clear()
            self.start_sync()
            entries = (
                *entries,
                ConfigEntry(
                    key=CONF_RESET_DB,
                    type=ConfigEntryType.LABEL,
                    label="The database has been reset.",
                ),
            )
        return entries

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
        if self.database:
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

        for media_type in media_types:
            for provider in self.providers:
                if provider.instance_id not in providers:
                    continue
                if not provider.library_supported(media_type):
                    continue
                self._start_provider_sync(provider, media_type)

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
        library_only: bool = False,
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
                    elif media_type == MediaType.AUDIOBOOK:
                        return SearchResults(audiobooks=[item])
                    elif media_type == MediaType.PODCAST:
                        return SearchResults(podcasts=[item])
                    else:
                        return SearchResults()

        # include results from library +  all (unique) music providers
        search_providers = [] if library_only else self.get_unique_providers()
        results_per_provider: list[SearchResults] = await asyncio.gather(
            self.search_library(search_query, media_types, limit=limit),
            *[
                self.search_provider(
                    search_query,
                    provider_instance,
                    media_types,
                    limit=limit,
                )
                for provider_instance in search_providers
            ],
        )
        # return result from all providers while keeping index
        # so the result is sorted as each provider delivered
        result = SearchResults(
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
            ][:limit],
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
            audiobooks=[
                item
                for sublist in zip_longest(*[x.audiobooks for x in results_per_provider])
                for item in sublist
                if item is not None
            ][:limit],
            podcasts=[
                item
                for sublist in zip_longest(*[x.podcasts for x in results_per_provider])
                for item in sublist
                if item is not None
            ][:limit],
        )

        # the search results should already be sorted by relevance
        # but we apply one extra round of sorting and that is to put exact name
        # matches and library items first
        result.artists = self._sort_search_result(search_query, result.artists)
        result.albums = self._sort_search_result(search_query, result.albums)
        result.tracks = self._sort_search_result(search_query, result.tracks)
        result.playlists = self._sort_search_result(search_query, result.playlists)
        result.radio = self._sort_search_result(search_query, result.radio)
        result.audiobooks = self._sort_search_result(search_query, result.audiobooks)
        result.podcasts = self._sort_search_result(search_query, result.podcasts)
        return result

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
        cache_category = CACHE_CATEGORY_MUSIC_SEARCH
        cache_base_key = prov.lookup_key
        cache_key = f"{search_query}.{limit}.{media_types_str}"

        if prov.is_streaming_provider and (
            cache := await self.mass.cache.get(
                cache_key, category=cache_category, base_key=cache_base_key
            )
        ):
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
                self.mass.cache.set(
                    cache_key,
                    result.to_dict(),
                    expiration=86400 * 7,
                    category=cache_category,
                    base_key=cache_base_key,
                )
            )
        return result

    async def search_library(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 10,
    ) -> SearchResults:
        """Perform search on the library.

        :param search_query: Search query
        :param media_types: A list of media_types to include.
        :param limit: number of items to return in the search (per type).
        """
        result = SearchResults()
        for media_type in media_types:
            ctrl = self.get_controller(media_type)
            search_results = await ctrl.search(search_query, "library", limit=limit)
            if search_results:
                if media_type == MediaType.ARTIST:
                    result.artists = search_results
                elif media_type == MediaType.ALBUM:
                    result.albums = search_results
                elif media_type == MediaType.TRACK:
                    result.tracks = search_results
                elif media_type == MediaType.PLAYLIST:
                    result.playlists = search_results
                elif media_type == MediaType.RADIO:
                    result.radio = search_results
                elif media_type == MediaType.AUDIOBOOK:
                    result.audiobooks = search_results
                elif media_type == MediaType.PODCAST:
                    result.podcasts = search_results
        return result

    @api_command("music/browse")
    async def browse(self, path: str | None = None) -> list[MediaItemType]:
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
        else:
            back_path = f"{provider_instance}://" + "/".join(sub_path.split("/")[:-1])
            prepend_items.append(
                BrowseFolder(
                    item_id="back",
                    provider=provider_instance,
                    path=back_path,
                    name="..",
                )
            )
        # limit -1 to account for the prepended items
        prov_items = await prov.browse(path=path)
        return prepend_items + prov_items

    @api_command("music/recently_played_items")
    async def recently_played(
        self, limit: int = 10, media_types: list[MediaType] | None = None
    ) -> list[ItemMapping]:
        """Return a list of the last played items."""
        if media_types is None:
            media_types = MediaType.ALL
        media_types_str = "(" + ",".join(f'"{x}"' for x in media_types) + ")"
        available_providers = ("library", *get_global_cache_value("unique_providers", []))
        available_providers_str = "(" + ",".join(f'"{x}"' for x in available_providers) + ")"
        query = (
            f"SELECT * FROM {DB_TABLE_PLAYLOG} "
            f"WHERE media_type in {media_types_str} AND fully_played = 1 "
            f"AND provider in {available_providers_str} "
            "ORDER BY timestamp DESC"
        )
        db_rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)
        result: list[ItemMapping] = []
        available_providers = ("library", *get_global_cache_value("available_providers", []))
        for db_row in db_rows:
            result.append(
                ItemMapping.from_dict(
                    {
                        "item_id": db_row["item_id"],
                        "provider": db_row["provider"],
                        "media_type": db_row["media_type"],
                        "name": db_row["name"],
                        "image": json_loads(db_row["image"]) if db_row["image"] else None,
                        "available": db_row["provider"] in available_providers,
                    }
                )
            )
        return result

    @api_command("music/in_progress_items")
    async def in_progress_items(self, limit: int = 10) -> list[ItemMapping]:
        """Return a list of the Audiobooks and PodcastEpisodes that are in progress."""
        available_providers = ("library", *get_global_cache_value("unique_providers", []))
        available_providers_str = "(" + ",".join(f'"{x}"' for x in available_providers) + ")"
        query = (
            f"SELECT * FROM {DB_TABLE_PLAYLOG} "
            f"WHERE media_type in ('audiobook', 'podcast_episode') AND fully_played = 0 "
            f"AND provider in {available_providers_str} "
            "AND seconds_played > 0 "
            "ORDER BY timestamp DESC"
        )
        db_rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)
        result: list[ItemMapping] = []
        for db_row in db_rows:
            result.append(
                ItemMapping.from_dict(
                    {
                        "item_id": db_row["item_id"],
                        "provider": db_row["provider"],
                        "media_type": db_row["media_type"],
                        "name": db_row["name"],
                        "image": json_loads(db_row["image"]) if db_row["image"] else None,
                        "available": db_row["provider"] in available_providers,
                    }
                )
            )
        return result

    @api_command("music/item_by_uri")
    async def get_item_by_uri(self, uri: str) -> MediaItemType:
        """Fetch MediaItem by uri."""
        media_type, provider_instance_id_or_domain, item_id = await parse_uri(uri)
        return await self.get_item(
            media_type=media_type,
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
        )

    @api_command("music/recommendations")
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get all recommendations."""
        recommendation_providers = [
            x for x in self.providers if ProviderFeature.RECOMMENDATIONS in x.supported_features
        ]
        results_per_provider: list[list[RecommendationFolder]] = await asyncio.gather(
            self._get_default_recommendations(),
            *[
                self._get_provider_recommendations(provider_instance)
                for provider_instance in recommendation_providers
            ],
        )
        # return result from all providers while keeping index
        # so the result is sorted as each provider delivered
        return [item for sublist in zip_longest(*results_per_provider) for item in sublist if item]

    @api_command("music/item")
    async def get_item(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> MediaItemType:
        """Get single music item by id and media type."""
        if provider_instance_id_or_domain == "database":
            # backwards compatibility - to remove when 2.0 stable is released
            provider_instance_id_or_domain = "library"
        if provider_instance_id_or_domain == "builtin":
            # handle special case of 'builtin' MusicProvider which allows us to play regular url's
            return await self.mass.get_provider("builtin").parse_item(item_id)
        if media_type == MediaType.PODCAST_EPISODE:
            # special case for podcast episodes
            return await self.podcasts.episode(item_id, provider_instance_id_or_domain)
        if media_type == MediaType.FOLDER:
            # special case for folders
            return BrowseFolder(
                item_id=item_id,
                provider=provider_instance_id_or_domain,
                name=item_id,
            )
        ctrl = self.get_controller(media_type)
        return await ctrl.get(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
        )

    @api_command("music/get_library_item")
    async def get_library_item_by_prov_id(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> MediaItemType | None:
        """Get single library music item by id and media type."""
        ctrl = self.get_controller(media_type)
        return await ctrl.get_library_item_by_prov_id(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
        )

    @api_command("music/favorites/add_item")
    async def add_item_to_favorites(
        self,
        item: str | MediaItemTypeOrItemMapping,
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
        )
        if full_item.provider != "library":
            full_item = await self.add_item_to_library(full_item)
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
        self, media_type: MediaType, library_item_id: str | int, recursive: bool = True
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
                # we simply try to remove it on the provider library
                # NOTE that the item may not be in the provider's library at all
                # so we need to be a bit forgiving here
                with suppress(NotImplementedError):
                    await prov_controller.library_remove(provider_mapping.item_id, item.media_type)
        await ctrl.remove_item_from_library(library_item_id, recursive)

    @api_command("music/library/add_item")
    async def add_item_to_library(
        self, item: str | MediaItemType, overwrite_existing: bool = False
    ) -> MediaItemType:
        """Add item (uri or mediaitem) to the library."""
        if isinstance(item, str):
            item = await self.get_item_by_uri(item)
        if isinstance(item, ItemMapping):
            item = await self.get_item(
                item.media_type,
                item.item_id,
                item.provider,
            )
        # add to provider(s) library first
        for prov_mapping in item.provider_mappings:
            provider = self.mass.get_provider(prov_mapping.provider_instance)
            if provider.library_edit_supported(item.media_type):
                prov_item = item
                prov_item.provider = prov_mapping.provider_instance
                prov_item.item_id = prov_mapping.item_id
                await provider.library_add(prov_item)
        # add (or overwrite) to library
        ctrl = self.get_controller(item.media_type)
        library_item = await ctrl.add_item_to_library(item, overwrite_existing)
        # perform full metadata scan (and provider match)
        await self.mass.metadata.update_metadata(library_item, overwrite_existing)
        return library_item

    async def refresh_items(self, items: list[MediaItemType]) -> None:
        """Refresh MediaItems to force retrieval of full info and matches.

        Creates background tasks to process the action.
        """
        async with TaskManager(self.mass) as tg:
            for media_item in items:
                tg.create_task(self.refresh_item(media_item))

    @api_command("music/refresh_item")
    async def refresh_item(
        self,
        media_item: str | MediaItemType,
    ) -> MediaItemType | None:
        """Try to refresh a mediaitem by requesting it's full object or search for substitutes."""
        if isinstance(media_item, str):
            # media item uri given
            media_item = await self.get_item_by_uri(media_item)

        media_type = media_item.media_type
        ctrl = self.get_controller(media_type)
        library_id = media_item.item_id if media_item.provider == "library" else None

        available_providers = get_global_cache_value("available_providers")
        if TYPE_CHECKING:
            available_providers = cast("set[str]", available_providers)

        # fetch the first (available) provider item
        for prov_mapping in sorted(
            media_item.provider_mappings, key=lambda x: x.priority, reverse=True
        ):
            if not self.mass.get_provider(prov_mapping.provider_instance):
                # ignore unavailable providers
                continue
            with suppress(MediaNotFoundError):
                media_item = await ctrl.get_provider_item(
                    prov_mapping.item_id,
                    prov_mapping.provider_instance,
                    force_refresh=True,
                )
                provider = media_item.provider
                item_id = media_item.item_id
                break
        else:
            # try to find a substitute using search
            searchresult = await self.search(media_item.name, [media_item.media_type], 20)
            if media_item.media_type == MediaType.ARTIST:
                result = searchresult.artists
            elif media_item.media_type == MediaType.ALBUM:
                result = searchresult.albums
            elif media_item.media_type == MediaType.TRACK:
                result = searchresult.tracks
            elif media_item.media_type == MediaType.PLAYLIST:
                result = searchresult.playlists
            elif media_item.media_type == MediaType.AUDIOBOOK:
                result = searchresult.audiobooks
            elif media_item.media_type == MediaType.PODCAST:
                result = searchresult.podcasts
            else:
                result = searchresult.radio
            for item in result:
                if item == media_item or item.provider == "library":
                    continue
                if item.available:
                    provider = item.provider
                    item_id = item.item_id
                    break
            else:
                # raise if we didn't find a substitute
                raise MediaNotFoundError(f"Could not find a substitute for {media_item.name}")
        # fetch full (provider) item
        media_item = await ctrl.get_provider_item(item_id, provider, force_refresh=True)
        # update library item if needed (including refresh of the metadata etc.)
        if library_id is None:
            return media_item
        library_item = await ctrl.update_item_in_library(library_id, media_item, overwrite=True)
        if library_item.media_type == MediaType.ALBUM:
            # update (local) album tracks
            for album_track in await self.albums.tracks(
                library_item.item_id, library_item.provider, True
            ):
                for prov_mapping in album_track.provider_mappings:
                    if not (prov := self.mass.get_provider(prov_mapping.provider_instance)):
                        continue
                    if prov.is_streaming_provider:
                        continue
                    with suppress(MediaNotFoundError):
                        prov_track = await prov.get_track(prov_mapping.item_id)
                        await self.mass.music.tracks.update_item_in_library(
                            album_track.item_id, prov_track
                        )

        await self.mass.metadata.update_metadata(library_item, force_refresh=True)
        return library_item

    async def set_loudness(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        loudness: float,
        album_loudness: float | None = None,
        media_type: MediaType = MediaType.TRACK,
    ) -> None:
        """Store (EBU-R128) Integrated Loudness Measurement for a mediaitem in db."""
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            return
        if loudness in (None, inf, -inf):
            # skip invalid values
            return
        values = {
            "item_id": item_id,
            "media_type": media_type.value,
            "provider": provider.lookup_key,
            "loudness": loudness,
        }
        if album_loudness not in (None, inf, -inf):
            values["loudness_album"] = album_loudness
        await self.database.insert_or_replace(DB_TABLE_LOUDNESS_MEASUREMENTS, values)

    async def get_loudness(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> tuple[float, float | None] | None:
        """Get (EBU-R128) Integrated Loudness Measurement for a mediaitem in db."""
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            return None
        db_row = await self.database.get_row(
            DB_TABLE_LOUDNESS_MEASUREMENTS,
            {
                "item_id": item_id,
                "media_type": media_type.value,
                "provider": provider.lookup_key,
            },
        )
        if db_row and db_row["loudness"] != inf and db_row["loudness"] != -inf:
            loudness = round(db_row["loudness"], 2)
            loudness_album = db_row["loudness_album"]
            loudness_album = (
                None if loudness_album in (None, inf, -inf) else round(loudness_album, 2)
            )
            return (loudness, loudness_album)

        return None

    @api_command("music/mark_played")
    async def mark_item_played(
        self,
        media_item: MediaItemType,
        fully_played: bool = True,
        seconds_played: int | None = None,
        is_playing: bool = False,
    ) -> None:
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()
        if (
            media_item.provider.startswith("builtin")
            and media_item.media_type != MediaType.PLAYLIST
        ):
            # we deliberately skip builtin provider items as those are often
            # one-off items like TTS or some sound effect etc.
            return

        # update generic playlog table (when not playing)
        if not is_playing:
            await self.database.insert(
                DB_TABLE_PLAYLOG,
                {
                    "item_id": media_item.item_id,
                    "provider": media_item.provider,
                    "media_type": media_item.media_type.value,
                    "name": media_item.name,
                    "image": serialize_to_json(media_item.image.to_dict())
                    if media_item.image
                    else None,
                    "fully_played": fully_played,
                    "seconds_played": seconds_played,
                    "timestamp": timestamp,
                },
                allow_replace=True,
            )

        # forward to provider(s) to sync resume state (e.g. for audiobooks)
        for prov_mapping in media_item.provider_mappings:
            if music_prov := self.mass.get_provider(prov_mapping.provider_instance):
                self.mass.create_task(
                    music_prov.on_played(
                        media_type=media_item.media_type,
                        prov_item_id=prov_mapping.item_id,
                        fully_played=fully_played,
                        position=seconds_played,
                        media_item=media_item,
                        is_playing=is_playing,
                    )
                )

        # also update playcount in library table (if fully played)
        if not fully_played or is_playing:
            return
        if not (ctrl := self.get_controller(media_item.media_type)):
            # skip non media items (e.g. plugin source)
            return
        db_item = await ctrl.get_library_item_by_prov_id(media_item.item_id, media_item.provider)
        if (
            not db_item
            and media_item.media_type in (MediaType.TRACK, MediaType.RADIO)
            and self.mass.config.get_raw_core_config_value(self.domain, CONF_ADD_LIBRARY_ON_PLAY)
        ):
            # handle feature to add to the lib on playback
            db_item = await self.add_item_to_library(media_item)

        if db_item:
            await self.database.execute(
                f"UPDATE {ctrl.db_table} SET play_count = play_count + 1, "
                f"last_played = {timestamp} WHERE item_id = {db_item.item_id}"
            )
        await self.database.commit()

    @api_command("music/mark_unplayed")
    async def mark_item_unplayed(
        self,
        media_item: MediaItemType,
    ) -> None:
        """Mark item as unplayed in playlog."""
        # update generic playlog table
        await self.database.delete(
            DB_TABLE_PLAYLOG,
            {
                "item_id": media_item.item_id,
                "provider": media_item.provider,
                "media_type": media_item.media_type.value,
            },
        )
        # forward to provider(s) to sync resume state (e.g. for audiobooks)
        for prov_mapping in media_item.provider_mappings:
            if music_prov := self.mass.get_provider(prov_mapping.provider_instance):
                self.mass.create_task(
                    music_prov.on_played(
                        media_type=media_item.media_type,
                        prov_item_id=prov_mapping.item_id,
                        fully_played=False,
                        position=0,
                        media_item=media_item,
                    )
                )
        # also update playcount in library table
        ctrl = self.get_controller(media_item.media_type)
        db_item = await ctrl.get_library_item_by_prov_id(media_item.item_id, media_item.provider)
        if db_item:
            await self.database.execute(
                f"UPDATE {ctrl.db_table} SET play_count = play_count - 1, "
                f"last_played = 0 WHERE item_id = {db_item.item_id}"
            )
            await self.database.commit()

    async def get_resume_position(self, media_item: Audiobook | PodcastEpisode) -> tuple[bool, int]:
        """
        Get progress (resume point) details for the given audiobook or episode.

        This is a separate call to ensure the resume position is always up-to-date
        and because many providers have this info present on a dedicated endpoint.

        Will be called right before playback starts to ensure the resume position is correct.

        Returns a boolean with the fully_played status
        and an integer with the resume position in ms.
        """
        for prov_mapping in media_item.provider_mappings:
            if not (music_prov := self.mass.get_provider(prov_mapping.provider_instance)):
                continue
            with suppress(NotImplementedError):
                return await music_prov.get_resume_position(
                    prov_mapping.item_id, media_item.media_type
                )
        # no provider info found, fallback to library playlog
        if db_entry := await self.mass.music.database.get_row(
            DB_TABLE_PLAYLOG,
            {
                "media_type": media_item.media_type.value,
                "item_id": media_item.item_id,
                "provider": media_item.provider,
            },
        ):
            resume_position_ms = (
                db_entry["seconds_played"] * 1000 if db_entry["seconds_played"] else 0
            )
            return (db_entry["fully_played"], resume_position_ms)

        return (False, 0)

    def get_controller(
        self, media_type: MediaType
    ) -> (
        ArtistsController
        | AlbumsController
        | TracksController
        | RadioController
        | PlaylistController
        | AudiobooksController
        | PodcastsController
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
        if media_type == MediaType.AUDIOBOOK:
            return self.audiobooks
        if media_type == MediaType.PODCAST:
            return self.podcasts
        if media_type == MediaType.PODCAST_EPISODE:
            return self.podcasts
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

    async def cleanup_provider(self, provider_instance: str) -> None:
        """Cleanup provider records from the database."""
        if provider_instance.startswith(("filesystem", "jellyfin", "plex", "opensubsonic")):
            # removal of a local provider can become messy very fast due to the relations
            # such as images pointing at the files etc. so we just reset the whole db
            # TODO: Handle this more gracefully in the future where we remove the provider
            # and traverse the database to also remove all related items.
            self.logger.warning(
                "Removal of local provider detected, issuing full database reset..."
            )
            await self._reset_database()
            return
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

        # always clear cache when a provider is removed
        await self.mass.cache.clear()

        # cleanup media items from db matched to deleted provider
        self.logger.info(
            "Removing provider %s from library, this can take a a while...",
            provider_instance,
        )
        errors = 0
        for ctrl in (
            # order is important here to recursively cleanup bottom up
            self.mass.music.radio,
            self.mass.music.playlists,
            self.mass.music.tracks,
            self.mass.music.albums,
            self.mass.music.artists,
            self.mass.music.podcasts,
            self.mass.music.audiobooks,
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

    async def _get_default_recommendations(self) -> list[RecommendationFolder]:
        """Return default recommendations."""
        return [
            RecommendationFolder(
                item_id="in_progress",
                provider="library",
                name="In progress",
                translation_key="in_progress_items",
                icon="mdi-motion-play",
                items=await self.in_progress_items(limit=10),
            ),
            RecommendationFolder(
                item_id="recently_played",
                provider="library",
                name="Recently played",
                translation_key="recently_played",
                icon="mdi-motion-play",
                items=await self.recently_played(limit=10),
            ),
            RecommendationFolder(
                item_id="random_artists",
                provider="library",
                name="Random artists",
                translation_key="random_artists",
                icon="mdi-account-music",
                items=await self.artists.library_items(limit=10, order_by="random_play_count"),
            ),
            RecommendationFolder(
                item_id="random_albums",
                provider="library",
                name="Random albums",
                translation_key="random_albums",
                icon="mdi-album",
                items=await self.albums.library_items(limit=10, order_by="random_play_count"),
            ),
            RecommendationFolder(
                item_id="recent_favorite_tracks",
                provider="library",
                name="Recently favorited tracks",
                translation_key="recent_favorite_tracks",
                icon="mdi-file-music",
                items=await self.tracks.library_items(
                    favorite=True, limit=10, order_by="timestamp_added"
                ),
            ),
            RecommendationFolder(
                item_id="favorite_playlists",
                provider="library",
                name="Favorite playlists",
                translation_key="favorite_playlists",
                icon="mdi-playlist-music",
                items=await self.playlists.library_items(
                    favorite=True, limit=10, order_by="random"
                ),
            ),
            RecommendationFolder(
                item_id="favorite_radio",
                provider="library",
                name="Favorite Radio stations",
                translation_key="favorite_radio",
                icon="mdi-access-point",
                items=await self.radio.library_items(favorite=True, limit=10, order_by="random"),
            ),
        ]

    async def _get_provider_recommendations(
        self, provider: MusicProvider
    ) -> list[RecommendationFolder]:
        """Return recommendations from a provider."""
        try:
            return await provider.recommendations()
        except Exception as err:
            self.logger.warning(
                "Error while fetching recommendations from %s: %s",
                provider.name,
                str(err),
                exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
            )
            return []

    def _start_provider_sync(self, provider: MusicProvider, media_type: MediaType) -> None:
        """Start sync task on provider and track progress."""
        # check if we're not already running a sync task for this provider/mediatype
        for sync_task in self.in_progress_syncs:
            if sync_task.provider_instance != provider.instance_id:
                continue
            if media_type in sync_task.media_types:
                self.logger.debug(
                    "Skip sync task for %s because another task is already in progress",
                    provider.name,
                )
                return

        async def run_sync() -> None:
            # Wrap the provider sync into a lock to prevent
            # race conditions when multiple providers are syncing at the same time.
            async with self._sync_lock:
                await provider.sync_library(media_type)
            # precache playlist tracks
            if media_type == MediaType.PLAYLIST:
                for playlist in await self.playlists.library_items(provider=provider.instance_id):
                    async for _ in self.playlists.tracks(playlist.item_id, playlist.provider):
                        pass

        # we keep track of running sync tasks
        task = self.mass.create_task(run_sync())
        sync_spec = SyncTask(
            provider_domain=provider.domain,
            provider_instance=provider.instance_id,
            media_types=(media_type,),
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

    def _schedule_sync(self) -> None:
        """Schedule the periodic sync."""
        sync_interval = self.config.get_value(CONF_SYNC_INTERVAL) * 60

        def run_scheduled_sync() -> None:
            # kickoff the sync job
            self.start_sync()
            # reschedule ourselves
            self.mass.loop.call_later(sync_interval, self._schedule_sync)

        # schedule the first sync run
        self.mass.loop.call_later(sync_interval, run_scheduled_sync)

    def _sort_search_result(
        self,
        search_query: str,
        items: Sequence[MediaItemTypeOrItemMapping],
    ) -> UniqueList[MediaItemTypeOrItemMapping]:
        """Sort search results on priority/preference."""
        scored_items: list[tuple[int, MediaItemTypeOrItemMapping]] = []
        # search results are already sorted by (streaming) providers on relevance
        # but we prefer exact name matches and library items so we simply put those
        # on top of the list.
        safe_title_str = create_safe_string(search_query)
        if " - " in search_query:
            artist, title_alt = search_query.split(" - ", 1)
            safe_title_alt = create_safe_string(title_alt)
            safe_artist_str = create_safe_string(artist)
        else:
            safe_artist_str = None
            safe_title_alt = None
        for item in items:
            score = 0
            if create_safe_string(item.name) not in (safe_title_str, safe_title_alt):
                # literal name match is mandatory to get a score at all
                continue
            # bonus point if artist provided and exact match
            if safe_artist_str:
                artist: Artist | ItemMapping
                for artist in getattr(item, "artists", []):
                    if create_safe_string(artist.name) == safe_artist_str:
                        score += 1
            # bonus point for library items
            if item.provider == "library":
                score += 1
            scored_items.append((score, item))
        scored_items.sort(key=lambda x: x[0], reverse=True)
        # combine it all with uniquelist, so this will deduplicated by default
        # note that streaming provider results are already (most likely) sorted on relevance
        # so we add all remaining items in their original order. We just prioritize
        # exact name matches and library items.
        return UniqueList([*[x[1] for x in scored_items], *items])

    async def _cleanup_database(self) -> None:
        """Perform database cleanup/maintenance."""
        self.logger.debug("Performing database cleanup...")
        # Remove playlog entries older than 90 days
        await self.database.delete_where_query(
            DB_TABLE_PLAYLOG, f"timestamp < strftime('%s','now') - {3600 * 24 * 90}"
        )
        # db tables cleanup
        for ctrl in (
            self.albums,
            self.artists,
            self.tracks,
            self.playlists,
            self.radio,
        ):
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
                # if the migration fails completely we reset the db
                # so the user at least can have a working situation back
                # a backup file is made with the previous version
                self.logger.error(
                    "Database migration failed - starting with a fresh library database, "
                    "a full rescan will be performed, this can take a while!",
                )
                if not isinstance(err, MusicAssistantError):
                    self.logger.exception(err)

                await self.database.close()
                await asyncio.to_thread(os.remove, db_path)
                self.database = DatabaseConnection(db_path)
                await self.database.setup()
                await self.mass.cache.clear()
                await self.__create_database_tables()

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
        # ruff: noqa: PLR0915
        self.logger.info(
            "Migrating database from version %s to %s", prev_version, DB_SCHEMA_VERSION
        )

        if prev_version < 7:
            raise MusicAssistantError("Database schema version too old to migrate")

        if prev_version <= 7:
            # remove redundant artists and provider_mappings columns
            for table in (
                DB_TABLE_TRACKS,
                DB_TABLE_ALBUMS,
                DB_TABLE_ARTISTS,
                DB_TABLE_RADIOS,
                DB_TABLE_PLAYLISTS,
            ):
                for column in ("artists", "provider_mappings"):
                    try:
                        await self.database.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                    except Exception as err:
                        if "no such column" in str(err):
                            continue
                        raise
            # add cache_checksum column to playlists
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLISTS} ADD COLUMN cache_checksum TEXT DEFAULT ''"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise

        if prev_version <= 8:
            # migrate track_loudness --> loudness_measurements
            async for db_row in self.database.iter_items("track_loudness"):
                if db_row["integrated"] == inf or db_row["integrated"] == -inf:
                    continue
                if db_row["provider"] in ("radiobrowser", "tunein"):
                    continue
                await self.database.insert_or_replace(
                    DB_TABLE_LOUDNESS_MEASUREMENTS,
                    {
                        "item_id": db_row["item_id"],
                        "media_type": "track",
                        "provider": db_row["provider"],
                        "loudness": db_row["integrated"],
                    },
                )
            await self.database.execute("DROP TABLE IF EXISTS track_loudness")

        if prev_version <= 10:
            # Recreate playlog table due to complete new layout
            await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_PLAYLOG}")
            await self.__create_database_tables()

        if prev_version <= 12:
            # Need to drop the NOT NULL requirement on podcasts.publisher and audiobooks.publisher
            # However, because there is no ALTER COLUMN support in sqlite, we will need
            # to create the tables again.
            await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_AUDIOBOOKS}")
            await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_PODCASTS}")
            await self.__create_database_tables()

        if prev_version <= 13:
            # migrate chapters in metadata
            # this is leftover mess from the old chapters implementation
            for db_row in await self.database.search(DB_TABLE_TRACKS, "position_start", "metadata"):
                metadata = json_loads(db_row["metadata"])
                metadata["chapters"] = None
                await self.database.update(
                    DB_TABLE_TRACKS,
                    {"item_id": db_row["item_id"]},
                    {"metadata": serialize_to_json(metadata)},
                )

        if prev_version <= 14:
            # Recreate playlog table due to complete new layout
            await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_PLAYLOG}")
            await self.__create_database_tables()

        if prev_version <= 15:
            # add search_name and search_sort_name columns to all tables
            # and populate them with the name and sort_name values
            # this is to allow for local/case independent searches
            for table in (
                DB_TABLE_TRACKS,
                DB_TABLE_ALBUMS,
                DB_TABLE_ARTISTS,
                DB_TABLE_RADIOS,
                DB_TABLE_PLAYLISTS,
                DB_TABLE_AUDIOBOOKS,
                DB_TABLE_PODCASTS,
            ):
                try:
                    await self.database.execute(
                        f"ALTER TABLE {table} ADD COLUMN search_name TEXT DEFAULT '' NOT NULL"
                    )
                    await self.database.execute(
                        f"ALTER TABLE {table} ADD COLUMN search_sort_name TEXT DEFAULT '' NOT NULL"
                    )
                except Exception as err:
                    if "duplicate column" not in str(err):
                        raise
                # migrate all existing values
                async for db_row in self.database.iter_items(table):
                    await self.database.update(
                        table,
                        {"item_id": db_row["item_id"]},
                        {
                            "search_name": create_safe_string(db_row["name"], True, True),
                            "search_sort_name": create_safe_string(db_row["sort_name"], True, True),
                        },
                    )

        if prev_version <= 16:
            # cleanup invalid release_date field in metadata
            for table in (
                DB_TABLE_TRACKS,
                DB_TABLE_ALBUMS,
                DB_TABLE_AUDIOBOOKS,
                DB_TABLE_PODCASTS,
            ):
                async for db_row in self.database.iter_items(table):
                    if '"release_date":null' in db_row["metadata"]:
                        continue
                    metadata = json_loads(db_row["metadata"])
                    try:
                        datetime.fromisoformat(metadata["release_date"])
                    except (KeyError, ValueError):
                        # this is not a valid date, so we set it to None
                        metadata["release_date"] = None
                        await self.database.update(
                            table,
                            {"item_id": db_row["item_id"]},
                            {
                                "metadata": serialize_to_json(metadata),
                            },
                        )

        # save changes
        await self.database.commit()

        # always clear the cache after a db migration
        await self.mass.cache.clear()

    async def _reset_database(self) -> None:
        """Reset the database."""
        await self.close()
        db_path = os.path.join(self.mass.storage_path, "library.db")
        await asyncio.to_thread(os.remove, db_path)
        await self._setup_database()
        # initiate full sync
        self.start_sync()

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
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_PLAYLOG}(
                [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                [item_id] TEXT NOT NULL,
                [provider] TEXT NOT NULL,
                [media_type] TEXT NOT NULL,
                [name] TEXT NOT NULL,
                [image] json,
                [timestamp] INTEGER DEFAULT 0,
                [fully_played] BOOLEAN,
                [seconds_played] INTEGER,
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
                    [search_name] TEXT NOT NULL,
                    [search_sort_name] TEXT NOT NULL
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
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL
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
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL
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
            [cache_checksum] TEXT DEFAULT '',
            [favorite] BOOLEAN DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL
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
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_AUDIOBOOKS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [version] TEXT,
            [favorite] BOOLEAN DEFAULT 0,
            [publisher] TEXT,
            [authors] json NOT NULL,
            [narrators] json NOT NULL,
            [metadata] json NOT NULL,
            [duration] INTEGER,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_PODCASTS}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [version] TEXT,
            [favorite] BOOLEAN DEFAULT 0,
            [publisher] TEXT,
            [total_episodes] INTEGER,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL
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

        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_LOUDNESS_MEASUREMENTS}(
                    [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [media_type] TEXT NOT NULL,
                    [item_id] TEXT NOT NULL,
                    [provider] TEXT NOT NULL,
                    [loudness] REAL,
                    [loudness_album] REAL,
                    UNIQUE(media_type,item_id,provider));"""
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
            DB_TABLE_AUDIOBOOKS,
            DB_TABLE_PODCASTS,
        ):
            # index on favorite column
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_favorite_idx on {db_table}(favorite);"
            )
            # index on name
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_name_idx on {db_table}(name);"
            )
            # index on search_name (=lowercase name without diacritics)
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_name_nocase_idx ON {db_table}(search_name);"
            )
            # index on sort_name
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_sort_name_idx on {db_table}(sort_name);"
            )
            # index on search_sort_name (=lowercase sort_name without diacritics)
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_search_sort_name_idx "
                f"ON {db_table}(search_sort_name);"
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
                f"CREATE INDEX IF NOT EXISTS {db_table}_play_count_idx on {db_table}(play_count);"
            )
            # index on last_played
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {db_table}_last_played_idx on {db_table}(last_played);"
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
        # index on loudness measurements table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_LOUDNESS_MEASUREMENTS}_idx "
            f"on {DB_TABLE_LOUDNESS_MEASUREMENTS}(media_type,item_id,provider);"
        )
        await self.database.commit()

    async def __create_database_triggers(self) -> None:
        """Create database triggers."""
        # triggers to auto update timestamps
        for db_table in (
            "artists",
            "albums",
            "tracks",
            "playlists",
            "radios",
            "audiobooks",
            "podcasts",
        ):
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
