"""MusicController: Orchestrates all data from music providers and sync to internal database."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections.abc import Iterable, Sequence
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from itertools import zip_longest
from math import inf
from typing import TYPE_CHECKING, Final, cast

import numpy as np
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
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.provider import SyncTask
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
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
    DB_TABLE_SMART_FADES_ANALYSIS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
    PROVIDERS_WITH_SHAREABLE_URLS,
)
from music_assistant.controllers.streams.smart_fades.fades import SMART_CROSSFADE_DURATION
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.compare import compare_strings, compare_version, create_safe_string
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.json import json_dumps, json_loads, serialize_to_json
from music_assistant.helpers.tags import split_artists
from music_assistant.helpers.uri import parse_uri
from music_assistant.helpers.util import TaskManager, parse_title_and_version
from music_assistant.models.core_controller import CoreController
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.smart_fades import SmartFadesAnalysis, SmartFadesAnalysisFragment

from .media.albums import AlbumsController
from .media.artists import ArtistsController
from .media.audiobooks import AudiobooksController
from .media.genres import GenreController
from .media.playlists import PlaylistController
from .media.podcasts import PodcastsController
from .media.radio import RadioController
from .media.tracks import TracksController

if TYPE_CHECKING:
    from music_assistant_models.auth import User
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.media_items import Audiobook, PodcastEpisode

    from music_assistant import MusicAssistant


CONF_RESET_DB = "reset_db"
DEFAULT_SYNC_INTERVAL = 12 * 60  # default sync interval in minutes
CONF_SYNC_INTERVAL = "sync_interval"
CONF_DELETED_PROVIDERS = "deleted_providers"
DB_SCHEMA_VERSION: Final[int] = 23

CACHE_CATEGORY_LAST_SYNC: Final[int] = 9
CACHE_CATEGORY_SEARCH_RESULTS: Final[int] = 10
LAST_PROVIDER_INSTANCE_SCAN: Final[str] = "last_provider_instance_scan"
PROVIDER_INSTANCE_SCAN_INTERVAL: Final[int] = 30 * 24 * 60 * 60  # one month in seconds


class MusicController(CoreController):
    """Several helpers around the musicproviders."""

    domain: str = "music"
    config: CoreConfig

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        self.cache = self.mass.cache
        self.artists = ArtistsController(self.mass)
        self.albums = AlbumsController(self.mass)
        self.tracks = TracksController(self.mass)
        self.radio = RadioController(self.mass)
        self.playlists = PlaylistController(self.mass)
        self.audiobooks = AudiobooksController(self.mass)
        self.podcasts = PodcastsController(self.mass)
        self.genres = GenreController(self.mass)
        self.in_progress_syncs: list[SyncTask] = []
        self._database: DatabaseConnection | None = None
        self._sync_lock = asyncio.Lock()
        self.manifest.name = "Music controller"
        self.manifest.description = (
            "Music Assistant's core controller which manages all music from all providers."
        )
        self.manifest.icon = "archive-music"

    @property
    def database(self) -> DatabaseConnection:
        """Return the database connection."""
        if self._database is None:
            raise RuntimeError("Database not initialized")
        return self._database

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        entries = (
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
            await self.start_sync()
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
        # make sure to finish any removal jobs
        for removed_provider in self.mass.config.get_raw_core_config_value(
            self.domain, CONF_DELETED_PROVIDERS, []
        ):
            await self.cleanup_provider(removed_provider)
        # schedule cleanup task for matching provider instances
        last_scan = cast("int", self.config.get_value(LAST_PROVIDER_INSTANCE_SCAN, 0))
        if time.time() - last_scan > PROVIDER_INSTANCE_SCAN_INTERVAL:
            self.mass.call_later(60, self.correct_multi_instance_provider_mappings)

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._database:
            await self._database.close()

    async def on_provider_loaded(self, provider: MusicProvider) -> None:
        """Handle logic when a provider is loaded."""
        await self.schedule_provider_sync(provider.instance_id)

    async def on_provider_unload(self, provider: MusicProvider) -> None:
        """Handle logic when a provider is (about to get) unloaded."""
        # make sure to stop any running sync tasks first
        for sync_task in self.in_progress_syncs:
            if sync_task.provider_instance == provider.instance_id:
                if sync_task.task:
                    sync_task.task.cancel()

    @property
    def providers(self) -> list[MusicProvider]:
        """
        Return all loaded/running MusicProviders (instances).

        Note that this applies user provider filters (for all user types).
        """
        user = get_current_user()
        user_provider_filter = user.provider_filter if user else None
        return [
            x
            for x in self.mass.providers
            if x.type == ProviderType.MUSIC
            and (not user_provider_filter or x.instance_id in user_provider_filter)
        ]

    @api_command("music/sync")
    async def start_sync(
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
                # handle mediatype specific sync config
                conf_key = f"library_sync_{media_type}s"
                sync_conf = await self.mass.config.get_provider_config_value(
                    provider.instance_id, conf_key
                )
                if not sync_conf:
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
        # use cache to avoid repeated searches
        search_providers = sorted(self.get_unique_providers())
        cache_provider_key = "library" if library_only else ",".join(search_providers)
        cache_key = f"{search_query}{'-'.join(sorted([mt.value for mt in media_types]))}-{limit}-{library_only}-{cache_provider_key}"  # noqa: E501
        if cache := await self.mass.cache.get(
            key=cache_key, provider=self.domain, category=CACHE_CATEGORY_SEARCH_RESULTS
        ):
            return cache
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
            # handle special case of direct shareable url search
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
        # handle normal global search by querying all providers
        results_per_provider: list[SearchResults] = []
        # always first search the library
        library_results = await self.search_library(search_query, media_types, limit=limit)
        results_per_provider.append(library_results)
        if not library_only:
            # create a set of all provider item ids already in library
            # this way we can avoid returning duplicates in the search results
            all_prov_item_ids = {
                (item.media_type, prov_mapping.provider_domain, prov_mapping.item_id)
                for result in (
                    library_results.artists,
                    library_results.albums,
                    library_results.tracks,
                    library_results.playlists,
                    library_results.audiobooks,
                    library_results.podcasts,
                )
                for item in result
                for prov_mapping in item.provider_mappings
            }
            # include results from library + all (unique) music providers
            results_per_provider += await asyncio.gather(
                *[
                    self._search_provider(
                        search_query,
                        provider_instance,
                        media_types,
                        limit=limit,
                        skip_item_ids=all_prov_item_ids,
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
        await self.mass.cache.set(
            key=cache_key,
            data=result,
            expiration=600,
            provider=self.domain,
            category=CACHE_CATEGORY_SEARCH_RESULTS,
        )
        return result

    async def _search_provider(
        self,
        search_query: str,
        provider_instance_id_or_domain: str,
        media_types: list[MediaType],
        limit: int = 10,
        skip_item_ids: set[tuple[MediaType, str, str]] | None = None,
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
        prov_search_results = await prov.search(
            search_query,
            media_types,
            limit,
        )
        if skip_item_ids:
            # filter out items already in skip_item_ids
            prov_search_results.artists = [
                item
                for item in prov_search_results.artists
                if (item.media_type, prov.domain, item.item_id) not in skip_item_ids
            ]
            prov_search_results.albums = [
                item
                for item in prov_search_results.albums
                if (item.media_type, prov.domain, item.item_id) not in skip_item_ids
            ]
            prov_search_results.tracks = [
                item
                for item in prov_search_results.tracks
                if (item.media_type, prov.domain, item.item_id) not in skip_item_ids
            ]
            prov_search_results.playlists = [
                item
                for item in prov_search_results.playlists
                if (item.media_type, prov.domain, item.item_id) not in skip_item_ids
            ]
            prov_search_results.audiobooks = [
                item
                for item in prov_search_results.audiobooks
                if (item.media_type, prov.domain, item.item_id) not in skip_item_ids
            ]
            prov_search_results.podcasts = [
                item
                for item in prov_search_results.podcasts
                if (item.media_type, prov.domain, item.item_id) not in skip_item_ids
            ]
        return prov_search_results

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
    async def browse(self, path: str | None = None) -> Sequence[MediaItemType | BrowseFolder]:
        """Browse Music providers."""
        if not path or path == "root":
            # root level; folder per provider
            root_items: list[BrowseFolder] = []
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
        prepend_items: list[BrowseFolder] = []
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
        self, limit: int = 10, media_types: list[MediaType] | None = None, all_users: bool = True
    ) -> list[ItemMapping]:
        """Return a list of the last played items."""
        if media_types is None:
            media_types = [
                MediaType.ALBUM,
                MediaType.AUDIOBOOK,
                MediaType.ARTIST,
                MediaType.PLAYLIST,
                MediaType.PODCAST,
                MediaType.FOLDER,
                MediaType.RADIO,
                MediaType.GENRE,
            ]
        media_types_str = "(" + ",".join(f'"{x}"' for x in media_types) + ")"
        available_providers = ("library", *self.get_unique_providers())
        available_providers_str = "(" + ",".join(f'"{x}"' for x in available_providers) + ")"
        query = (
            f"SELECT * FROM {DB_TABLE_PLAYLOG} "
            f"WHERE media_type in {media_types_str} AND fully_played = 1 "
            f"AND provider in {available_providers_str} "
        )
        if not all_users and (user := get_current_user()):
            query += f"AND userid = '{user.user_id}' "
        query += "ORDER BY timestamp DESC"
        db_rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)
        result: list[ItemMapping] = []
        available_providers = ("library", *get_global_cache_value("available_providers", []))

        # Get user provider filter if set
        user = get_current_user()
        user_provider_filter = user.provider_filter if user and user.provider_filter else None

        for db_row in db_rows:
            provider = db_row["provider"]
            # Apply user provider filter
            if user_provider_filter and provider not in user_provider_filter:
                continue
            result.append(
                ItemMapping.from_dict(
                    {
                        "item_id": db_row["item_id"],
                        "provider": provider,
                        "media_type": db_row["media_type"],
                        "name": db_row["name"],
                        "image": json_loads(db_row["image"]) if db_row["image"] else None,
                        "available": provider in available_providers,
                    }
                )
            )
        return result

    @api_command("music/recently_added_tracks")
    async def recently_added_tracks(self, limit: int = 10) -> list[Track]:
        """Return a list of the last added tracks."""
        return await self.tracks.library_items(limit=limit, order_by="timestamp_added_desc")

    @api_command("music/in_progress_items")
    async def in_progress_items(
        self, limit: int = 10, all_users: bool = False
    ) -> list[ItemMapping]:
        """Return a list of the Audiobooks and PodcastEpisodes that are in progress."""
        available_providers = ("library", *self.get_unique_providers())
        available_providers_str = "(" + ",".join(f'"{x}"' for x in available_providers) + ")"
        query = (
            f"SELECT * FROM {DB_TABLE_PLAYLOG} "
            f"WHERE media_type in ('audiobook', 'podcast_episode') AND fully_played = 0 "
            f"AND provider in {available_providers_str} "
            "AND seconds_played > 0 "
        )
        if not all_users and (user := get_current_user()):
            query += f"AND userid = '{user.user_id}' "

        query += "ORDER BY timestamp DESC"
        db_rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)
        result: list[ItemMapping] = []

        # Get user provider filter if set
        user = get_current_user()
        user_provider_filter = user.provider_filter if user and user.provider_filter else None

        for db_row in db_rows:
            provider = db_row["provider"]
            # Apply user provider filter
            if user_provider_filter and provider not in user_provider_filter:
                continue
            result.append(
                ItemMapping.from_dict(
                    {
                        "item_id": db_row["item_id"],
                        "provider": provider,
                        "media_type": db_row["media_type"],
                        "name": db_row["name"],
                        "image": json_loads(db_row["image"]) if db_row["image"] else None,
                        "available": provider in available_providers,
                    }
                )
            )
        return result

    async def get_playlog_provider_item_ids(
        self, provider_instance_id: str, limit: int = 0, userid: str | None = None
    ) -> list[tuple[MediaType, str]]:
        """Return a list of MediaType and provider_item_id of items in playlog of provider."""
        # check if there is a provider user
        # this method is not available in the frontend, so no need to check for session users.
        user: User | None = None
        if userid:
            # userid overridden by parameter
            user = await self.mass.webserver.auth.get_user(userid)
        elif provider_user := await self._get_user_for_provider(provider_instance_id):
            # based on configured provider filter we can try to find a user
            user = provider_user

        query = (
            f"SELECT * FROM {DB_TABLE_PLAYLOG} "
            "WHERE media_type in ('audiobook', 'podcast_episode') "
            f"AND provider in ('library','{provider_instance_id}')"
        )

        if user:
            # NOTE: if no user was found, we will return playlog items for all users
            query += f" AND userid = '{user.user_id}'"
        db_rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)

        result: list[tuple[MediaType, str]] = []
        for db_row in db_rows:
            if db_row["provider"] == "library":
                # If the provider is library, we need to make sure that the item
                # is part of the passed provider_instance_id.
                # A podcast_episode cannot be in the provider_mappings
                # so these entries must be audiobooks.
                subquery = (
                    f"SELECT * FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                    f"WHERE media_type = 'audiobook' AND item_id = {db_row['item_id']} "
                    f"AND provider_instance = '{provider_instance_id}'"
                )
                subrow = await self.mass.music.database.get_rows_from_query(subquery)
                if len(subrow) != 1:
                    continue
                result.append((MediaType.AUDIOBOOK, subrow[0]["provider_item_id"]))
                continue
            # non library - item id is provider_item_id
            result.append((MediaType(db_row["media_type"]), db_row["item_id"]))

        return result

    @api_command("music/item_by_uri")
    async def get_item_by_uri(self, uri: str) -> MediaItemType | BrowseFolder:
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
    ) -> MediaItemType | BrowseFolder:
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
        item: str | MediaItemType | ItemMapping,
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
        )
        if full_item.provider != "library":
            full_item = await self.add_item_to_library(full_item)
        # set favorite in library db
        ctrl = self.get_controller(item.media_type)
        await ctrl.set_favorite(
            full_item.item_id,
            True,
        )
        # forward to provider(s) if needed
        for prov_mapping in full_item.provider_mappings:
            provider = self.mass.get_provider(prov_mapping.provider_instance)
            if not provider.library_favorites_edit_supported(full_item.media_type):
                continue
            await provider.set_favorite(prov_mapping.item_id, full_item.media_type, True)

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
        # forward to provider(s) if needed
        full_item = await ctrl.get_library_item(library_item_id)
        for prov_mapping in full_item.provider_mappings:
            provider = self.mass.get_provider(prov_mapping.provider_instance)
            if not provider.library_favorites_edit_supported(full_item.media_type):
                continue
            self.mass.create_task(provider.set_favorite(prov_mapping.item_id, media_type, False))

    @api_command("music/library/remove_item")
    async def remove_item_from_library(
        self, media_type: MediaType, library_item_id: str | int, recursive: bool = True
    ) -> None:
        """
        Remove item from the library.

        Destructive! Will remove the item and all dependants.
        """
        ctrl = self.get_controller(media_type)
        # remove from provider(s) library
        full_item = await ctrl.get_library_item(library_item_id)
        for prov_mapping in full_item.provider_mappings:
            if not prov_mapping.in_library:
                continue
            provider = self.mass.get_provider(prov_mapping.provider_instance)
            if not provider.library_edit_supported(full_item.media_type):
                continue
            if not provider.library_sync_back_enabled(full_item.media_type):
                continue
            prov_mapping.in_library = False
            self.mass.create_task(provider.library_remove(prov_mapping.item_id, media_type))
        # remove from library
        await ctrl.remove_item_from_library(library_item_id, recursive)

    @api_command("music/library/add_item")
    async def add_item_to_library(
        self, item: str | MediaItemType, overwrite_existing: bool = False
    ) -> MediaItemType:
        """Add item (uri or mediaitem) to the library."""
        # ensure we have a full item
        if isinstance(item, str):
            full_item = await self.get_item_by_uri(item)
        # For builtin provider (manual URLs), use the provided item directly
        # to preserve custom modifications (name, images, etc.)
        # For other providers, fetch fresh to ensure data validity
        elif item.provider == "builtin":
            full_item = item
        else:
            full_item = await self.get_item(
                item.media_type,
                item.item_id,
                item.provider,
            )
        # add to provider(s) library first
        for prov_mapping in full_item.provider_mappings:
            provider = self.mass.get_provider(prov_mapping.provider_instance)
            if not provider.library_edit_supported(full_item.media_type):
                continue
            if not provider.library_sync_back_enabled(full_item.media_type):
                continue
            prov_item = deepcopy(full_item) if full_item.provider == "library" else full_item
            prov_item.provider = prov_mapping.provider_instance
            prov_item.item_id = prov_mapping.item_id
            prov_mapping.in_library = True
            self.mass.create_task(provider.library_add(prov_item))
        # add (or overwrite) to library
        ctrl = self.get_controller(full_item.media_type)
        library_item = await ctrl.add_item_to_library(full_item, overwrite_existing)
        # perform full metadata scan
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
    async def refresh_item(  # noqa: PLR0915
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
        await ctrl.match_providers(library_item)
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
        # prefer domain for streaming providers as the catalog is the same across instances
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        values = {
            "item_id": item_id,
            "media_type": media_type.value,
            "provider": prov_key,
            "loudness": loudness,
        }
        if album_loudness not in (None, inf, -inf):
            values["loudness_album"] = album_loudness
        await self.database.insert_or_replace(DB_TABLE_LOUDNESS_MEASUREMENTS, values)

    async def set_smart_fades_analysis(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        analysis: SmartFadesAnalysis,
    ) -> None:
        """Store Smart Fades BPM analysis for a track in db."""
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            return
        if (
            analysis.duration <= 0.75 * SMART_CROSSFADE_DURATION
            or analysis.bpm <= 0
            or analysis.confidence < 0
        ):
            # skip invalid values, we skip analysis that were performed on
            # a short amount of audio as those are often unreliable
            return
        beats_json = await asyncio.to_thread(lambda: json_dumps(analysis.beats.tolist()))
        downbeats_json = await asyncio.to_thread(lambda: json_dumps(analysis.downbeats.tolist()))
        # prefer domain for streaming providers as the catalog is the same across instances
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        values = {
            "fragment": analysis.fragment.value,
            "item_id": item_id,
            "provider": prov_key,
            "bpm": analysis.bpm,
            "beats": beats_json,
            "downbeats": downbeats_json,
            "confidence": analysis.confidence,
            "duration": analysis.duration,
        }
        await self.database.insert_or_replace(DB_TABLE_SMART_FADES_ANALYSIS, values)

    async def get_smart_fades_analysis(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        fragment: SmartFadesAnalysisFragment,
    ) -> SmartFadesAnalysis | None:
        """Get Smart Fades BPM analysis for a track from db."""
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            return None
        # prefer domain for streaming providers as the catalog is the same across instances
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        db_row = await self.database.get_row(
            DB_TABLE_SMART_FADES_ANALYSIS,
            {
                "item_id": item_id,
                "provider": prov_key,
                "fragment": fragment.value,
            },
        )
        if db_row and db_row["bpm"] > 0:
            beats = await asyncio.to_thread(lambda: np.array(json_loads(db_row["beats"])))
            downbeats = await asyncio.to_thread(lambda: np.array(json_loads(db_row["downbeats"])))
            return SmartFadesAnalysis(
                fragment=SmartFadesAnalysisFragment(db_row["fragment"]),
                bpm=float(db_row["bpm"]),
                beats=beats,
                downbeats=downbeats,
                confidence=float(db_row["confidence"]),
                duration=float(db_row["duration"]),
            )
        return None

    async def get_loudness(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        media_type: MediaType = MediaType.TRACK,
    ) -> tuple[float, float | None] | None:
        """Get (EBU-R128) Integrated Loudness Measurement for a mediaitem in db."""
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            return None
        # prefer domain for streaming providers as the catalog is the same across instances
        prov_key = provider.domain if provider.is_streaming_provider else provider.instance_id
        db_row = await self.database.get_row(
            DB_TABLE_LOUDNESS_MEASUREMENTS,
            {
                "item_id": item_id,
                "media_type": media_type.value,
                "provider": prov_key,
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
        userid: str | None = None,
    ) -> None:
        """
        Mark item as played in playlog.

        :param media_item: The media item to mark as played.
        :param fully_played: If True, mark the item as fully played.
        :param seconds_played: The number of seconds played.
        :param is_playing: If True, the item is currently playing.
        :param userid: The user ID to mark the item as played for (instead of the current user).
        """
        timestamp = utc_timestamp()
        if (
            media_item.provider.startswith("builtin")
            and media_item.media_type != MediaType.PLAYLIST
        ):
            # we deliberately skip builtin provider items as those are often
            # one-off items like TTS or some sound effect etc.
            return

        params = {
            "item_id": media_item.item_id,
            "provider": media_item.provider,
            "media_type": media_item.media_type.value,
            "name": media_item.name,
            "image": serialize_to_json(media_item.image.to_dict()) if media_item.image else None,
            "fully_played": fully_played,
            "seconds_played": seconds_played,
            "timestamp": timestamp,
        }
        # try to figure out the user that triggered the action
        user: User | None = None
        if userid:
            # userid overridden by parameter
            user = await self.mass.webserver.auth.get_user(userid)
        elif session_user := get_current_user():
            # this is the active session user that triggered the action
            user = session_user
        elif provider_user := await self._get_user_for_provider(media_item.provider_mappings):
            # based on configured provider filter we can try to find a user
            user = provider_user

        if user:
            # NOTE: if no user was found, we will alter the playlog for all users
            params["userid"] = user.user_id

        # update generic playlog table (when not playing)
        if not is_playing:
            await self.database.insert(
                DB_TABLE_PLAYLOG,
                params,
                allow_replace=True,
            )

        # forward to provider(s) to sync resume state (e.g. for audiobooks)
        for prov_mapping in media_item.provider_mappings:
            if (
                user
                and user.provider_filter
                and prov_mapping.provider_instance not in user.provider_filter
            ):
                continue
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
        userid: str | None = None,
    ) -> None:
        """
        Mark item as unplayed in playlog.

        :param media_item: The media item to mark as unplayed.
        :param all_users: If True, mark the item as unplayed for all users.
        :param userid: The user ID to mark the item as unplayed for (instead of the current user).
        """
        params = {
            "item_id": media_item.item_id,
            "provider": media_item.provider,
            "media_type": media_item.media_type.value,
        }
        # try to figure out the user that triggered the action
        user: User | None = None
        if userid:
            # userid overridden by parameter
            user = await self.mass.webserver.auth.get_user(userid)
        elif session_user := get_current_user():
            # this is the active session user that triggered the action
            user = session_user
        elif provider_user := await self._get_user_for_provider(media_item.provider_mappings):
            # based on configured provider filter we can try to find a user
            user = provider_user

        if user:
            # NOTE: if no user was found, we will alter the playlog for all users
            params["userid"] = user.user_id

        await self.database.delete(DB_TABLE_PLAYLOG, params)
        # forward to provider(s) to sync resume state (e.g. for audiobooks)
        for prov_mapping in media_item.provider_mappings:
            if (
                user
                and user.provider_filter
                and prov_mapping.provider_instance not in user.provider_filter
            ):
                continue
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

    @api_command("music/track_by_name")
    async def get_track_by_name(
        self,
        track_name: str,
        artist_name: str | None = None,
        album_name: str | None = None,
        track_version: str | None = None,
    ) -> Track | None:
        """Get a track by its name, optionally with artist and album."""
        if track_version is None:
            track_name, version = parse_title_and_version(track_name)
        search_query = f"{artist_name} - {track_name}" if artist_name else track_name
        search_result = await self.mass.music.search(
            search_query=search_query,
            media_types=[MediaType.TRACK],
        )
        for allow_item_mapping in (False, True):
            for search_track in search_result.tracks:
                is_track = isinstance(search_track, Track)
                if not allow_item_mapping and not is_track:
                    continue
                if not compare_strings(track_name, search_track.name):
                    continue
                if not compare_version(version, search_track.version):
                    continue
                # check optional artist(s)
                if artist_name and is_track:
                    for artist in search_track.artists:
                        if compare_strings(artist_name, artist.name, False):
                            break
                    else:
                        # no artist match found: abort
                        continue
                # check optional album
                if (
                    album_name
                    and is_track
                    and not compare_strings(album_name, search_track.album.name, False)
                ):
                    # no album match found: abort
                    continue
                    # if we reach this, we found a match
                if not isinstance(search_track, Track):
                    # ensure we return an actual Track object
                    return await self.mass.music.tracks.get(
                        item_id=search_track.item_id,
                        provider_instance_id_or_domain=search_track.provider,
                    )
                return search_track

        # try to handle case where something is appended to the title
        for splitter in ("•", "-", "|", "(", "["):
            if splitter in track_name:
                return await self.get_track_by_name(
                    track_name=track_name.split(splitter)[0].strip(),
                    artist_name=artist_name,
                    album_name=None,
                    track_version=track_version,
                )
        # try to handle case where multiple artists are given as single string
        if artist_name and (artists := split_artists(artist_name, True)) and len(artists) > 1:
            for artist in artists:
                return await self.get_track_by_name(
                    track_name=track_name,
                    artist_name=artist.split(splitter)[0].strip(),
                    album_name=None,
                    track_version=track_version,
                )
        # allow non-exact album match as fallback
        if album_name:
            return await self.get_track_by_name(
                track_name=track_name,
                artist_name=artist_name,
                album_name=None,
                track_version=track_version,
            )
        # no match found
        return None

    async def get_resume_position(
        self, media_item: Audiobook | PodcastEpisode, userid: str | None = None
    ) -> tuple[bool, int]:
        """
        Get progress (resume point) details for the given audiobook or episode.

        This is a separate call to ensure the resume position is always up-to-date
        and because many providers have this info present on a dedicated endpoint.

        Will be called right before playback starts to ensure the resume position is correct.

        Returns a boolean with the fully_played status
        and an integer with the resume position in ms.
        """
        provider_fully_played = False
        provider_position_ms = 0

        # Try to get position from providers
        for prov_mapping in media_item.provider_mappings:
            if not (
                provider := self.mass.get_provider(
                    prov_mapping.provider_instance, provider_type=MusicProvider
                )
            ):
                continue
            with suppress(NotImplementedError):
                (
                    provider_fully_played,
                    provider_position_ms,
                ) = await provider.get_resume_position(prov_mapping.item_id, media_item.media_type)
                break  # Use first provider that returns data

        # Get MA's internal position from playlog
        ma_fully_played = False
        ma_position_ms = 0
        params = {
            "media_type": media_item.media_type.value,
            "item_id": media_item.item_id,
            "provider": media_item.provider,
        }
        if userid:
            params["userid"] = userid
        if db_entry := await self.database.get_row(DB_TABLE_PLAYLOG, params):
            ma_position_ms = db_entry["seconds_played"] * 1000 if db_entry["seconds_played"] else 0
            ma_fully_played = db_entry["fully_played"]

        # Return the higher position to ensure users never lose progress
        if ma_position_ms >= provider_position_ms:
            return ma_fully_played, ma_position_ms
        else:
            return provider_fully_played, provider_position_ms

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
        | GenreController
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
        if media_type == MediaType.GENRE:
            return self.genres
        raise NotImplementedError

    def get_provider_instances(
        self, domain: str, return_unavailable: bool = False
    ) -> list[MusicProvider]:
        """
        Return all provider instances for a given domain.

        Note that this skips user filters so may only be called from internal code.
        """
        return cast(
            "list[MusicProvider]",
            self.mass.get_provider_instances(domain, return_unavailable, ProviderType.MUSIC),
        )

    def get_unique_providers(self) -> list[str]:
        """
        Return all unique MusicProvider (instance or domain) ids.

        This will return a set of provider instance ids but will only return
        a single instance_id per streaming provider domain.

        Applies user provider filters (for non-admin users).
        """
        processed_domains: set[str] = set()
        # Get user provider filter if set
        user = get_current_user()
        user_provider_filter = user.provider_filter if user and user.provider_filter else None
        result: list[str] = []
        for provider in self.providers:
            if provider.is_streaming_provider and provider.domain in processed_domains:
                continue
            if user_provider_filter and provider.instance_id not in user_provider_filter:
                continue
            result.append(provider.instance_id)
            processed_domains.add(provider.domain)
        return result

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

    async def schedule_provider_sync(self, provider_instance_id: str) -> None:
        """Schedule Library sync for given provider."""
        if not (provider := self.mass.get_provider(provider_instance_id)):
            return
        self.unschedule_provider_sync(provider.instance_id)
        for media_type in MediaType:
            if not provider.library_supported(media_type):
                continue
            await self._schedule_provider_mediatype_sync(provider, media_type, True)

    def unschedule_provider_sync(self, provider_instance_id: str) -> None:
        """Unschedule Library sync for given provider."""
        # cancel all scheduled sync tasks
        for media_type in MediaType:
            key = f"sync_{provider_instance_id}_{media_type.value}"
            self.mass.cancel_timer(key)
        # cancel any running sync tasks
        for sync_task in self.in_progress_syncs:
            if sync_task.provider_instance == provider_instance_id:
                sync_task.task.cancel()

    def match_provider_instances(
        self,
        item: MediaItemType,
    ) -> bool:
        """Match all provider instances for the given item."""
        mappings_added = False
        for provider_mapping in list(item.provider_mappings):
            if provider_mapping.is_unique:
                # unique mapping, no need to map
                continue
            if not (provider := self.mass.get_provider(provider_mapping.provider_instance)):
                continue
            if not provider.is_streaming_provider:
                continue
            provider_instances = self.get_provider_instances(
                provider.domain, return_unavailable=True
            )
            if len(provider_instances) <= 1:
                # only a single instance, no need to map
                continue
            for prov_instance in provider_instances:
                if prov_instance.instance_id == provider.instance_id:
                    continue
                if any(
                    pm.provider_instance == prov_instance.instance_id
                    for pm in item.provider_mappings
                ):
                    # mapping already exists
                    continue
                # create additional mapping for other provider instances of the same provider
                item.provider_mappings.add(
                    ProviderMapping(
                        item_id=provider_mapping.item_id,
                        provider_domain=provider.domain,
                        provider_instance=prov_instance.instance_id,
                        available=provider_mapping.available,
                        is_unique=provider_mapping.is_unique,
                        audio_format=provider_mapping.audio_format,
                        url=provider_mapping.url,
                        details=provider_mapping.details,
                        in_library=None,
                    )
                )
                mappings_added = True
        return mappings_added

    @api_command("music/add_provider_mapping")
    async def add_provider_mapping(
        self, media_type: MediaType, db_id: str, mapping: ProviderMapping
    ) -> None:
        """Add provider mapping to the given library item."""
        ctrl = self.get_controller(media_type)
        await ctrl.add_provider_mappings(db_id, [mapping])

    @api_command("music/remove_provider_mapping")
    async def remove_provider_mapping(
        self, media_type: MediaType, db_id: str, mapping: ProviderMapping
    ) -> None:
        """Remove provider mapping from the given library item."""
        ctrl = self.get_controller(media_type)
        await ctrl.remove_provider_mapping(db_id, mapping.provider_instance, mapping.item_id)

    @api_command("music/match_providers")
    async def match_providers(self, media_type: MediaType, db_id: str) -> None:
        """Search for mappings on all providers for the given library item."""
        ctrl = self.get_controller(media_type)
        db_item = await ctrl.get_library_item(db_id)
        await ctrl.match_providers(db_item)

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
                item_id="recently_added_tracks",
                provider="library",
                name="Recently added tracks",
                translation_key="recently_added_tracks",
                icon="music-note-plus",
                items=await self.tracks.library_items(limit=10, order_by="timestamp_added_desc"),
            ),
            RecommendationFolder(
                item_id="recently_added_albums",
                provider="library",
                name="Recently added albums",
                translation_key="recently_added_albums",
                icon="music-note-plus",
                items=await self.albums.library_items(limit=10, order_by="timestamp_added_desc"),
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
                    favorite=True, limit=10, order_by="timestamp_modified_desc"
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
                translation_key="favorite_radio_stations",
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
            if sync_task.task.done():
                continue
            if media_type in sync_task.media_types:
                self.logger.debug(
                    "Skip sync task for %s/%ss because another task is already in progress",
                    provider.name,
                    media_type.value,
                )
                return

        async def run_sync() -> None:
            # Wrap the provider sync into a lock to prevent
            # race conditions when multiple providers are syncing at the same time.
            async with self._sync_lock:
                await provider.sync_library(media_type)

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
                    "Sync task for %s/%ss completed with errors",
                    provider.name,
                    media_type.value,
                    exc_info=task_err if self.logger.isEnabledFor(10) else None,
                )
            else:
                self.logger.info("Sync task for %s/%ss completed", provider.name, media_type.value)
            self.mass.signal_event(EventType.SYNC_TASKS_UPDATED, data=self.in_progress_syncs)
            self.mass.create_task(
                self.mass.cache.set(
                    key=media_type.value,
                    data=self.mass.loop.time(),
                    provider=provider.instance_id,
                    category=CACHE_CATEGORY_LAST_SYNC,
                )
            )
            # schedule db cleanup after sync
            if not self.in_progress_syncs:
                self.mass.create_task(self._cleanup_database())
            # reschedule next execution
            self.mass.create_task(self._schedule_provider_mediatype_sync(provider, media_type))

        task.add_done_callback(on_sync_task_done)
        return

    def _sort_search_result(
        self,
        search_query: str,
        items: Sequence[MediaItemType | ItemMapping],
    ) -> UniqueList[MediaItemType | ItemMapping]:
        """Sort search results on priority/preference."""
        scored_items: list[tuple[int, MediaItemType | ItemMapping]] = []
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

    async def _schedule_provider_mediatype_sync(
        self, provider: MusicProvider, media_type: MediaType, is_initial: bool = False
    ) -> None:
        """Schedule Library sync for given provider and media type."""
        job_key = f"sync_{provider.instance_id}_{media_type.value}"
        # cancel any existing timers
        self.mass.cancel_timer(job_key)
        # handle mediatype specific sync config
        conf_key = f"library_sync_{media_type}s"
        sync_conf = await self.mass.config.get_provider_config_value(provider.instance_id, conf_key)
        if not sync_conf:
            return
        conf_key = f"provider_sync_interval_{media_type.value}s"
        sync_interval = await self.mass.config.get_provider_config_value(
            provider.instance_id, conf_key, return_type=int
        )
        if sync_interval <= 0:
            # sync disabled for this media type
            return
        sync_interval = sync_interval * 60  # config interval is in minutes - convert to seconds

        if is_initial:
            # schedule the first sync run
            initial_interval = 10
            if last_sync := await self.mass.cache.get(
                key=media_type.value,
                provider=provider.instance_id,
                category=CACHE_CATEGORY_LAST_SYNC,
            ):
                initial_interval += max(0, sync_interval - (self.mass.loop.time() - last_sync))
            sync_interval = initial_interval

        self.mass.call_later(
            sync_interval,
            self._start_provider_sync,
            provider,
            media_type,
            task_id=job_key,
        )

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
        self._database = DatabaseConnection(db_path)
        await self._database.setup()

        # always create db tables if they don't exist to prevent errors trying to access them later
        await self.__create_database_tables()
        try:
            if db_row := await self._database.get_row(DB_TABLE_SETTINGS, {"key": "version"}):
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

                await self._database.close()
                await asyncio.to_thread(os.remove, db_path)
                self._database = DatabaseConnection(db_path)
                await self._database.setup()
                await self.mass.cache.clear()
                await self.__create_database_tables()

        # store current schema version
        await self._database.insert_or_replace(
            DB_TABLE_SETTINGS,
            {"key": "version", "value": str(DB_SCHEMA_VERSION), "type": "str"},
        )
        # create indexes and triggers if needed
        await self.__create_database_indexes()
        await self.__create_database_triggers()
        # compact db
        self.logger.debug("Compacting database...")
        try:
            await self._database.vacuum()
        except Exception as err:
            self.logger.warning("Database vacuum failed: %s", str(err))
        else:
            self.logger.debug("Compacting database done")

    async def __migrate_database(self, prev_version: int) -> None:  # noqa: PLR0915
        """Perform a database migration."""
        self.logger.info(
            "Migrating database from version %s to %s", prev_version, DB_SCHEMA_VERSION
        )

        if prev_version < 15:
            raise MusicAssistantError("Database schema version too old to migrate")

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
                    await self._database.execute(
                        f"ALTER TABLE {table} ADD COLUMN search_name TEXT DEFAULT '' NOT NULL"
                    )
                    await self._database.execute(
                        f"ALTER TABLE {table} ADD COLUMN search_sort_name TEXT DEFAULT '' NOT NULL"
                    )
                except Exception as err:
                    if "duplicate column" not in str(err):
                        raise
                # migrate all existing values
                async for db_row in self._database.iter_items(table):
                    await self._database.update(
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
                async for db_row in self._database.iter_items(table):
                    if '"release_date":null' in db_row["metadata"]:
                        continue
                    metadata = json_loads(db_row["metadata"])
                    try:
                        datetime.fromisoformat(metadata["release_date"])
                    except (KeyError, ValueError):
                        # this is not a valid date, so we set it to None
                        metadata["release_date"] = None
                        await self._database.update(
                            table,
                            {"item_id": db_row["item_id"]},
                            {
                                "metadata": serialize_to_json(metadata),
                            },
                        )

        if prev_version <= 17:
            # migrate triggers to auto update timestamps
            # it had an error in the previous version where it was not created
            for db_table in (
                "artists",
                "albums",
                "tracks",
                "playlists",
                "radios",
                "audiobooks",
                "podcasts",
            ):
                await self._database.execute(f"DROP TRIGGER IF EXISTS update_{db_table}_timestamp;")

        if prev_version <= 18:
            # add in_library column to provider_mappings table
            await self._database.execute(
                f"ALTER TABLE {DB_TABLE_PROVIDER_MAPPINGS} ADD COLUMN in_library "
                "BOOLEAN NOT NULL DEFAULT 0;"
            )
            # migrate existing entries in provider_mappings which are filesystem
            await self._database.execute(
                f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
                "WHERE provider_domain in ('filesystem_local', 'filesystem_smb');"
            )

        if prev_version <= 20:
            # drop column cache_checksum from playlists table
            # this is no longer used and is a leftover from previous designs
            try:
                await self._database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLISTS} DROP COLUMN cache_checksum"
                )
            except Exception as err:
                if "no such column" not in str(err):
                    raise

        if prev_version <= 21:
            # drop table for smart fades analysis - it will be recreated with needed columns
            await self._database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_SMART_FADES_ANALYSIS}")
            await self.__create_database_tables()

        if prev_version <= 22:
            # add userid column to playlog table
            try:
                await self._database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN userid TEXT"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise
            # Note: SQLite doesn't support modifying constraints directly
            # The UNIQUE constraint will be updated when the table is recreated
            # For now, we'll keep the old constraint and add a new one via unique index
            try:
                await self._database.execute(f"DROP INDEX IF EXISTS {DB_TABLE_PLAYLOG}_unique_idx")
                await self._database.execute(
                    f"CREATE UNIQUE INDEX {DB_TABLE_PLAYLOG}_unique_idx "
                    f"ON {DB_TABLE_PLAYLOG}(item_id,provider,media_type,userid)"
                )
            except Exception as err:
                # If we can't create the index due to duplicate entries, log and continue
                self.logger.warning("Could not create unique index on playlog: %s", err)

        if prev_version <= 23:
            # add is_unique column to provider_mappings table
            try:
                await self._database.execute(
                    f"ALTER TABLE {DB_TABLE_PROVIDER_MAPPINGS} ADD COLUMN is_unique BOOLEAN"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise

        # save changes
        await self._database.commit()

        # always clear the cache after a db migration
        await self.mass.cache.clear()

    async def _reset_database(self) -> None:
        """Reset the database."""
        await self.close()
        db_path = os.path.join(self.mass.storage_path, "library.db")
        await asyncio.to_thread(os.remove, db_path)
        await self._setup_database()
        # initiate full sync
        await self.start_sync()

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
                [userid] TEXT NOT NULL,
                UNIQUE(item_id, provider, media_type, userid));"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_ALBUMS}(
                    [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [name] TEXT NOT NULL,
                    [sort_name] TEXT NOT NULL,
                    [version] TEXT,
                    [album_type] TEXT NOT NULL,
                    [year] INTEGER,
                    [favorite] BOOLEAN NOT NULL DEFAULT 0,
                    [metadata] json NOT NULL,
                    [external_ids] json NOT NULL,
                    [play_count] INTEGER NOT NULL DEFAULT 0,
                    [last_played] INTEGER NOT NULL DEFAULT 0,
                    [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                    [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
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
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
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
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
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
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
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
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
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
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [publisher] TEXT,
            [authors] json NOT NULL,
            [narrators] json NOT NULL,
            [metadata] json NOT NULL,
            [duration] INTEGER,
            [external_ids] json NOT NULL,
            [play_count] INTEGER DEFAULT 0,
            [last_played] INTEGER DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
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
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [publisher] TEXT,
            [total_episodes] INTEGER NOT NULL,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [play_count] INTEGER NOT NULL DEFAULT 0,
            [last_played] INTEGER NOT NULL DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
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
            [available] BOOLEAN NOT NULL DEFAULT 1,
            [in_library] BOOLEAN NOT NULL DEFAULT 0,
            [is_unique] BOOLEAN,
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

        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_SMART_FADES_ANALYSIS}(
                    [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [item_id] TEXT NOT NULL,
                    [provider] TEXT NOT NULL,
                    [fragment] INTEGER NOT NULL,
                    [bpm] REAL NOT NULL,
                    [beats] TEXT NOT NULL,
                    [downbeats] TEXT NOT NULL,
                    [confidence] REAL NOT NULL,
                    [duration] REAL,
                    [analysis_version] INTEGER DEFAULT 1,
                    [timestamp_created] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                    UNIQUE(item_id,provider,fragment));"""
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
        await self.database.execute(
            "CREATE INDEX IF NOT EXISTS "
            f"{DB_TABLE_PROVIDER_MAPPINGS}_media_type_provider_instance_library_idx "
            f"on {DB_TABLE_PROVIDER_MAPPINGS}(media_type,provider_instance,in_library);"
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
        # index on smart fades analysis table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_SMART_FADES_ANALYSIS}_idx "
            f"on {DB_TABLE_SMART_FADES_ANALYSIS}(item_id,provider,fragment);"
        )
        # unique index on playlog table
        await self.database.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {DB_TABLE_PLAYLOG}_unique_idx "
            f"on {DB_TABLE_PLAYLOG}(item_id,provider,media_type,userid);"
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
                AFTER UPDATE ON {db_table}
                BEGIN
                    UPDATE {db_table} SET timestamp_modified=cast(strftime('%s','now') as int)
                    WHERE rowid = new.rowid;
                END;
                """
            )
        await self.database.commit()

    async def correct_multi_instance_provider_mappings(self) -> None:
        """Correct provider mappings for multi-instance providers."""
        self.logger.debug("Correcting provider mappings for multi-instance providers...")
        multi_instance_providers: set[str] = set()
        for provider in self.providers:
            if len(self.get_provider_instances(provider.domain)) > 1:
                multi_instance_providers.add(provider.instance_id)
        if not multi_instance_providers:
            return  # no multi-instance providers found, nothing to do

        for ctrl in (
            self.albums,
            self.artists,
            self.tracks,
            self.playlists,
            self.radio,
            self.audiobooks,
            self.podcasts,
        ):
            async for db_item in ctrl.iter_library_items(provider=list(multi_instance_providers)):
                if self.match_provider_instances(db_item):
                    await ctrl.update_item_in_library(db_item.item_id, db_item)
                # prevent overwhelming the event loop
                await asyncio.sleep(0.2)
        self.mass.config.set_raw_core_config_value(
            self.domain, LAST_PROVIDER_INSTANCE_SCAN, int(time.time())
        )
        self.logger.debug("Provider mappings correction done")

    async def _get_user_for_provider(
        self, provider_mappings_or_instance_id: Iterable[ProviderMapping] | str
    ) -> User | None:
        """Try to get the MA User based on provider mappings and provider filter."""
        all_users = await self.mass.webserver.auth.list_users()
        for mapping_or_instance_id in provider_mappings_or_instance_id:
            for user in all_users:
                if not user.provider_filter:
                    continue
                if isinstance(mapping_or_instance_id, str):
                    if provider_mappings_or_instance_id in user.provider_filter:
                        return user
                elif mapping_or_instance_id.provider_instance in user.provider_filter:
                    return user
        return None
