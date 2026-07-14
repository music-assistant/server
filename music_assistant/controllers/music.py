"""MusicController: Orchestrates all data from music providers and sync to internal database."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, Final, cast

from music_assistant_models.background_task import BackgroundTask, TaskMetadata, TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    EventType,
    MediaType,
    ProviderFeature,
    ProviderType,
    TaskStatus,
)
from music_assistant_models.errors import (
    InvalidDataError,
    InvalidProviderID,
    InvalidProviderURI,
    MediaNotFoundError,
    MusicAssistantError,
    UnsupportedFeaturedException,
)
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    Genre,
    ItemMapping,
    MediaItemType,
    Playlist,
    Podcast,
    ProviderMapping,
    Radio,
    RecommendationFolder,
    SearchResults,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_BACK,
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIOBOOKS,
    DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_GENRES,
    DB_TABLE_LOUDNESS_MEASUREMENTS,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PODCASTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_RADIOS,
    DB_TABLE_SETTINGS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
    DEFAULT_GENRE_MAPPING,
    GENRE_ICONS_DIR_NAME,
    LOUDNESS_MEASUREMENT_MIN_LUFS,
    PROVIDERS_WITH_SHAREABLE_URLS,
    VACUUM_MIN_RECLAIM_RATIO,
)
from music_assistant.controllers.tasks.context import update_current_task_progress_text
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.compare import compare_strings, compare_version, create_safe_string
from music_assistant.helpers.database import UNSET, DatabaseConnection
from music_assistant.helpers.datetime import (
    from_utc_timestamp,
    local_clock_time_to_utc,
    utc_timestamp,
)
from music_assistant.helpers.json import json_dumps, json_loads, serialize_to_json
from music_assistant.helpers.tags import split_artists
from music_assistant.helpers.uri import parse_uri
from music_assistant.helpers.util import TaskManager, parse_optional_bool, parse_title_and_version
from music_assistant.models.core_controller import CoreController
from music_assistant.models.music_provider import MusicProvider
from music_assistant.models.plugin import PluginProvider

from .media.albums import AlbumsController
from .media.artists import ArtistsController
from .media.audiobooks import AudiobooksController
from .media.base import SUPPRESS_MEDIA_ITEM_UPDATES
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
    from music_assistant.controllers.media.base import MediaControllerBase
    from music_assistant.models import ProviderInstanceType
    from music_assistant.models.metadata_provider import MetadataProvider
    from music_assistant.models.provider import Provider
    from music_assistant.providers.builtin import BuiltinProvider


CONF_RESET_DB = "reset_db"
DEFAULT_SYNC_INTERVAL = 12 * 60  # default sync interval in minutes
CONF_SYNC_INTERVAL = "sync_interval"
CONF_DELETED_PROVIDERS = "deleted_providers"
DB_SCHEMA_VERSION: Final[int] = 43
# tracks longer that this will not be included in radio mode
RADIO_TRACK_MAX_DURATION_SECS: Final[int] = 20 * 60
_DYNAMIC_RADIO_BASE_SAMPLE_SIZE: Final[int] = 5
_DYNAMIC_RADIO_DYNAMIC_TARGET: Final[int] = 50

CACHE_CATEGORY_SEARCH_RESULTS: Final[int] = 10
DATABASE_CLEANUP_TASK_ID: Final[str] = "music_database_cleanup"
PROVIDER_MAPPING_CORRECTION_TASK_ID: Final[str] = "music_provider_mapping_correction"
MUSIC_SYNC_COMPLETION_CHECK_TASK_ID: Final[str] = "music_sync_completion_check"


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
        entries: tuple[ConfigEntry, ...] = (
            ConfigEntry(
                key=CONF_RESET_DB,
                type=ConfigEntryType.ACTION,
                label="Reset library database",
                description="This will issue a full reset of the library "
                "database and trigger a full sync. Only use this option as a last resort "
                "if you are seeing issues with the library database.",
                category="generic",
                advanced=True,
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
        for removed_provider in cast(
            "list[str]",
            self.mass.config.get_raw_core_config_value(self.domain, CONF_DELETED_PROVIDERS, []),
        ):
            await self.cleanup_provider(removed_provider)

    async def post_setup(self) -> None:
        """Handle logic after all core controllers have been set up."""
        self._register_database_cleanup_task()
        self._register_provider_mapping_correction_task()
        self.genres.register_scheduled_scan_task()

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._database:
            await self._database.close()

    async def on_provider_loaded(self, provider: MusicProvider) -> None:
        """Handle logic when a provider is loaded."""
        await self.schedule_provider_sync(provider.instance_id)

    async def on_provider_unload(self, provider: MusicProvider) -> None:
        """Handle logic when a provider is (about to get) unloaded."""
        self.unschedule_provider_sync(provider.instance_id)

    @property
    def providers(self) -> list[MusicProvider]:
        """
        Return all loaded/running MusicProviders (instances).

        Note that this applies user provider filters (for all user types).
        """
        return cast(
            "list[MusicProvider]",
            [
                x
                for x in self._apply_user_provider_filter(self.mass.providers)
                if x.type == ProviderType.MUSIC
            ],
        )

    def _apply_user_provider_filter(
        self,
        providers: Iterable[ProviderInstanceType],
    ) -> list[ProviderInstanceType]:
        """Filter providers by the current user's music provider filter."""
        user = get_current_user()
        user_provider_filter = user.provider_filter if user else None
        if not user_provider_filter:
            return list(providers)
        return [
            p
            for p in providers
            if p.type != ProviderType.MUSIC or p.instance_id in user_provider_filter
        ]

    @api_command("music/sync")
    async def start_sync(
        self,
        media_types: list[MediaType] | None = None,
        providers: list[str] | None = None,
    ) -> list[BackgroundTask]:
        """
        Start running the sync of (all or selected) musicproviders.

        media_types: only sync these media types. None for all.
        providers: only sync these provider instances. None for all.
        """
        tasks: list[BackgroundTask] = []
        if media_types is None:
            media_types = MediaType.ALL
        if providers is None:
            providers = [x.instance_id for x in self.providers]

        for media_type in media_types:
            for provider in self.providers:
                if provider.instance_id not in providers:
                    continue
                if not self.library_supported(provider, media_type):
                    continue
                # handle mediatype specific sync config
                conf_key = f"library_sync_{media_type}s"
                sync_conf: ConfigValueType = await self.mass.config.get_provider_config_value(
                    provider.instance_id, conf_key
                )
                if not sync_conf:
                    continue
                await self._schedule_provider_mediatype_sync(provider, media_type, True)
                task_id = self._get_sync_task_id(provider, media_type)
                try:
                    tasks.append(self.mass.tasks.run_task(task_id))
                except InvalidDataError:
                    tasks.append(
                        self.mass.tasks.run_background_task(
                            task_id=task_id,
                            name=self._get_sync_task_name(provider, media_type),
                            handler=self._create_provider_sync_handler(provider, media_type),
                            translation_key=self._get_sync_task_translation_key(media_type),
                            translation_args=[provider.name],
                            user_id=(user.user_id if (user := get_current_user()) else None),
                            metadata=self._get_sync_task_metadata(provider, media_type),
                            allow_retry=True,
                            priority=True,
                        )
                    )
        return tasks

    @property
    def active_sync_tasks(self) -> list[BackgroundTask]:
        """Return provider sync tasks that are currently pending or running."""
        return [
            task
            for task in self.mass.tasks.get_tasks_by_metadata(task_domain="music_sync")
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
        ]

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
        plugin_search_providers = [
            p.instance_id
            for p in self.mass.get_providers_supporting_feature(
                ProviderFeature.SEARCH,
                priority=(ProviderType.PLUGIN,),
            )
        ]
        search_providers = sorted(self.get_unique_providers() + plugin_search_providers)
        cache_provider_key = "library" if library_only else ",".join(search_providers)
        cache_key = f"{search_query}{'-'.join(sorted([mt.value for mt in media_types]))}-{limit}-{library_only}-{cache_provider_key}"
        if cache := await self.mass.cache.get(
            key=cache_key,
            provider=self.domain,
            category=CACHE_CATEGORY_SEARCH_RESULTS,
            base_class=SearchResults,
        ):
            return cast("SearchResults", cache)
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
                        return SearchResults(artists=[cast("Artist", item)])
                    if media_type == MediaType.ALBUM:
                        return SearchResults(albums=[cast("Album", item)])
                    if media_type == MediaType.TRACK:
                        return SearchResults(tracks=[cast("Track", item)])
                    if media_type == MediaType.PLAYLIST:
                        return SearchResults(playlists=[cast("Playlist", item)])
                    if media_type == MediaType.AUDIOBOOK:
                        return SearchResults(audiobooks=[cast("Audiobook", item)])
                    if media_type == MediaType.PODCAST:
                        return SearchResults(podcasts=[cast("Podcast", item)])
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
                for items in (
                    library_results.artists,
                    library_results.albums,
                    library_results.tracks,
                    library_results.playlists,
                    library_results.audiobooks,
                    library_results.podcasts,
                )
                for item in items
                for prov_mapping in cast("MediaItemType", item).provider_mappings
            }
            # include results from library + all (unique) music providers
            # one failing provider must not break the entire search,
            # so exceptions are logged and excluded from the results
            gather_results = await asyncio.gather(
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
                return_exceptions=True,
            )
            for res in gather_results:
                if isinstance(res, SearchResults):
                    results_per_provider.append(res)
                else:
                    self.logger.error("Search on provider failed", exc_info=res)
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
            data=result.to_dict(),
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
        prov = self.mass.get_provider(provider_instance_id_or_domain, provider_type=MusicProvider)
        if not prov:
            return SearchResults()
        if ProviderFeature.SEARCH not in prov.supported_features:
            return SearchResults()

        # create safe search string
        search_query = search_query.replace("/", " ").replace("'", "")
        # guard against a failing provider: return empty results instead of
        # raising, so a global search can still succeed with the other providers
        try:
            prov_search_results = await prov.search(
                search_query,
                media_types,
                limit,
            )
        except MusicAssistantError as err:
            self.logger.warning("Search on provider %s failed: %s", prov.name, str(err))
            return SearchResults()
        except Exception as err:
            self.logger.error("Search on provider %s failed: %s", prov.name, str(err), exc_info=err)
            return SearchResults()
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
                    result.artists = cast("list[Artist]", search_results)
                elif media_type == MediaType.ALBUM:
                    result.albums = cast("list[Album]", search_results)
                elif media_type == MediaType.TRACK:
                    result.tracks = cast("list[Track]", search_results)
                elif media_type == MediaType.PLAYLIST:
                    result.playlists = cast("list[Playlist]", search_results)
                elif media_type == MediaType.RADIO:
                    result.radio = cast("list[Radio]", search_results)
                elif media_type == MediaType.AUDIOBOOK:
                    result.audiobooks = cast("list[Audiobook]", search_results)
                elif media_type == MediaType.PODCAST:
                    result.podcasts = cast("list[Podcast]", search_results)
        return result

    @api_command("music/browse")
    async def browse(
        self, path: str | None = None
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse Music providers."""
        if not path or path == "root":
            # root level; folder per provider that declares BROWSE
            root_items: list[MediaItemType | BrowseFolder] = []
            providers_with_browse = self.mass.get_providers_supporting_feature(
                ProviderFeature.BROWSE
            )
            for prov in self._apply_user_provider_filter(providers_with_browse):
                root_items.append(
                    BrowseFolder(
                        item_id="root",
                        provider=prov.domain,
                        path=f"{prov.instance_id}://",
                        uri=f"{prov.instance_id}://",
                        name=prov.name,
                    )
                )
            # AudioSource providers surface at root like regular providers; a
            # provider with a single user-initiable source is promoted to that
            # source directly so it's playable in one tap.
            audio_source_providers = self.mass.get_providers_supporting_feature(
                ProviderFeature.AUDIO_SOURCE
            )
            for prov in self._apply_user_provider_filter(audio_source_providers):
                if not isinstance(prov, PluginProvider):
                    continue
                initiable = [
                    source for source in await prov.get_audio_sources() if source.can_initiate
                ]
                if not initiable:
                    continue
                if len(initiable) == 1:
                    root_items.append(initiable[0])
                else:
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
        browse_prov = self.mass.get_provider(provider_instance)
        # handle regular provider listing, always add back folder first
        if not browse_prov or not sub_path:
            prepend_items.append(
                BrowseFolder(item_id="root", provider="library", path="root", name="..")
            )
            if not browse_prov:
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
        # AudioSource providers don't implement browse(); list their initiable sources directly
        if (
            isinstance(browse_prov, PluginProvider)
            and ProviderFeature.AUDIO_SOURCE in browse_prov.supported_features
        ):
            initiable_items: list[MediaItemType | BrowseFolder] = [
                source for source in await browse_prov.get_audio_sources() if source.can_initiate
            ]
            return [*prepend_items, *initiable_items]
        # limit -1 to account for the prepended items
        prov_items = await cast("MusicProvider", browse_prov).browse(path=path)
        return [*prepend_items, *prov_items]

    @api_command("music/recently_played_items")
    async def recently_played(
        self,
        limit: int = 10,
        media_types: list[MediaType] | None = None,
        userid: str | None = None,
        queue_id: str | None = None,
        fully_played_only: bool = True,
        user_initiated_only: bool = False,
        played_after_timestamp: int | None = None,
    ) -> list[ItemMapping]:
        """Return a list of the last played items.

        :param limit: Maximum number of items to return.
        :param media_types: Filter by media types.
        :param userid: Filter by specific user ID.
        :param queue_id: Filter by specific queue ID.
        :param fully_played_only: If True, only return fully played items.
        :param user_initiated_only: If True, only return items initiated by the user.
        :param played_after_timestamp: If set, only return items played at or after this
            epoch-seconds timestamp.
        """
        if media_types is None:
            media_types = MediaType.ALL
        media_types_str = "(" + ",".join(f'"{x}"' for x in media_types) + ")"
        available_providers = ("library", *self.get_unique_providers())
        available_providers_str = "(" + ",".join(f'"{x}"' for x in available_providers) + ")"
        query = (
            f"SELECT * FROM {DB_TABLE_PLAYLOG} "
            f"WHERE media_type in {media_types_str} "
            f"AND provider in {available_providers_str} "
        )
        params: dict[str, Any] = {}
        if fully_played_only:
            query += "AND fully_played = 1 "
        if user_initiated_only:
            query += "AND user_initiated = 1 "
        if userid:
            query += "AND userid = :userid "
            params["userid"] = userid
        elif user := get_current_user():
            query += "AND userid = :userid "
            params["userid"] = user.user_id
        if queue_id:
            query += "AND queue_id = :queue_id "
            params["queue_id"] = queue_id
        if played_after_timestamp is not None:
            query += "AND timestamp >= :played_after_timestamp "
            params["played_after_timestamp"] = played_after_timestamp
        query += "ORDER BY timestamp DESC"
        db_rows = await self.mass.music.database.get_rows_from_query(
            query, params=params or None, limit=limit
        )
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

        # An audiobook can be part of the library, in contrast to podcast episodes.
        # We then need to check the provider mappings table.
        one_week_ago = int(utc_timestamp()) - (7 * 86400)
        query = (
            "SELECT p.item_id, p.media_type, p.name, p.image, p.provider "
            f"FROM {DB_TABLE_PLAYLOG} p "
            "WHERE p.media_type IN ('audiobook', 'podcast_episode') "
            "AND p.fully_played = 0 "
            "AND p.seconds_played > 0 "
            f"AND (p.media_type != 'podcast_episode' OR p.timestamp >= {one_week_ago}) "
        )
        query += (
            "AND ( "
            "CASE WHEN p.provider = 'library' THEN "
            f"EXISTS (SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} m "
            "WHERE m.item_id = p.item_id AND m.media_type = p.media_type "
        )
        if not all_users and (user := get_current_user()):
            filter_for_str = available_providers_str
            if user.provider_filter:
                filter_for_str = "(" + ",".join(f'"{x}"' for x in user.provider_filter) + ")"
            query += (
                f"AND m.provider_instance IN {filter_for_str} "
                f"AND m.provider_instance IN {available_providers_str} "
                ") "
                f"ELSE (p.provider IN {filter_for_str} AND p.provider IN {available_providers_str})"
                "END "
                ") "
                f"AND p.userid = '{user.user_id}' "
            )
        else:
            # for a library item, we still have to verify via the provider mapping table
            # that the provider is available
            query += (
                f"AND m.provider_instance IN {available_providers_str} "
                ") "
                f"ELSE p.provider IN {available_providers_str} "
                "END "
                ") "
            )
        query += "ORDER BY timestamp DESC"

        db_rows = await self.mass.music.database.get_rows_from_query(query, limit=limit)
        result: list[ItemMapping] = []
        for db_row in db_rows:
            provider = db_row["provider"]
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
    async def get_item_by_uri(
        self, uri: str, allow_update_metadata: bool = False
    ) -> MediaItemType | BrowseFolder:
        """Fetch MediaItem by uri."""
        media_type, provider_instance_id_or_domain, item_id = await parse_uri(uri)
        return await self.get_item(
            media_type=media_type,
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            allow_update_metadata=allow_update_metadata,
        )

    @api_command("music/recommendations")
    async def recommendations(self) -> list[RecommendationFolder]:
        """Get all recommendations."""
        providers_with_recommendations = self.mass.get_providers_supporting_feature(
            ProviderFeature.RECOMMENDATIONS,
        )
        recommendation_providers = self._apply_user_provider_filter(providers_with_recommendations)
        results_per_provider: list[list[RecommendationFolder]] = await asyncio.gather(
            self._get_default_recommendations(),
            *[
                self._get_provider_recommendations(
                    cast("MusicProvider | MetadataProvider | PluginProvider", provider_instance)
                )
                for provider_instance in recommendation_providers
            ],
        )
        # return result from all providers while keeping index
        # so the result is sorted as each provider delivered
        return [item for sublist in zip_longest(*results_per_provider) for item in sublist if item]

    async def get_dynamic_radio_tracks(
        self,
        seeds: list[MediaItemType],
        *,
        include_base_tracks: bool = False,
        target_size: int = 25,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Generate a dynamic radio track pool from one or more seed media items.

        :param seeds: Seed media items (Track, Artist, Album, Playlist, ...) used as sources.
        :param include_base_tracks: When True, interleave the sampled base tracks into the result
            using the BDDBDD pattern. When False, only similar tracks are returned.
        :param target_size: Maximum number of dynamic (similar) tracks to sample into the result.
            When ``include_base_tracks`` is True, base tracks are added on top of this cap.
        :param preferred_provider_instances: Provider instance IDs preferred for similar lookups.
        :raises UnsupportedFeaturedException: When no base tracks could be derived from any seed.
        """
        seen: set[Track] = set()
        available_base_tracks: list[Track] = []
        for seed in random.sample(seeds, len(seeds)):
            ctrl = self.get_controller(seed.media_type)
            try:
                base_tracks_for_seed = await ctrl.radio_mode_base_tracks(
                    seed,  # type: ignore[arg-type]
                    preferred_provider_instances,
                )
            except UnsupportedFeaturedException:
                continue
            for track in base_tracks_for_seed:
                if track not in seen:
                    seen.add(track)
                    available_base_tracks.append(track)
        if not available_base_tracks:
            raise UnsupportedFeaturedException("Radio mode not available for source items")

        base_tracks = random.sample(
            available_base_tracks,
            min(_DYNAMIC_RADIO_BASE_SAMPLE_SIZE, len(available_base_tracks)),
        )
        dynamic_tracks: set[Track] = set()
        for allow_lookup in (False, True):
            if len(dynamic_tracks) >= _DYNAMIC_RADIO_DYNAMIC_TARGET:
                break
            for base_track in base_tracks:
                try:
                    similar = await self.tracks.similar_tracks(
                        base_track.item_id,
                        base_track.provider,
                        allow_lookup=allow_lookup,
                        preferred_provider_instances=preferred_provider_instances,
                    )
                except MediaNotFoundError:
                    continue
                for track in similar:
                    if track not in base_tracks and track.duration <= RADIO_TRACK_MAX_DURATION_SECS:
                        dynamic_tracks.add(track)
                if len(dynamic_tracks) >= _DYNAMIC_RADIO_DYNAMIC_TARGET:
                    break

        result: list[Track] = []
        dynamic_tracks_list = list(dynamic_tracks)
        if include_base_tracks:
            result.append(base_tracks[0])
            if len(base_tracks) > 1:
                for base_track in base_tracks[1:]:
                    result.append(base_track)
                    if len(dynamic_tracks_list) > 2:
                        result += random.sample(dynamic_tracks_list, 2)
                    else:
                        result += dynamic_tracks_list
        remaining_dynamic = [t for t in dynamic_tracks_list if t not in result]
        if remaining_dynamic:
            result += random.sample(remaining_dynamic, min(len(remaining_dynamic), target_size))
        return result

    @api_command("music/item")
    async def get_item(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
        allow_update_metadata: bool = True,
    ) -> MediaItemType | BrowseFolder:
        """Get single music item by id and media type."""
        if provider_instance_id_or_domain == "database":
            # backwards compatibility - to remove when 2.0 stable is released
            provider_instance_id_or_domain = "library"
        if provider_instance_id_or_domain == "builtin":
            # handle special case of 'builtin' MusicProvider which allows us to play regular url's
            builtin_prov = cast("BuiltinProvider", self.mass.get_provider("builtin"))
            return await builtin_prov.parse_item(item_id)
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
        if media_type == MediaType.AUDIO_SOURCE:
            # AudioSources are not library-backed; resolve them through the owning
            # plugin provider's get_audio_sources() catalog. Returning the live
            # MediaItem lets play_media create a queue item the standard way.
            prov = self.mass.get_provider(provider_instance_id_or_domain)
            if isinstance(prov, PluginProvider):
                for source in await prov.get_audio_sources():
                    if source.item_id == item_id:
                        return source
            raise MediaNotFoundError(
                f"AudioSource {provider_instance_id_or_domain}/{item_id} not found"
            )
        ctrl = self.get_controller(media_type)
        return await ctrl.get(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            allow_update_metadata=allow_update_metadata,
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
            # Inspect the URI's media_type first so a stale audio-source URI
            # whose plugin is unloaded gives the honest rejection error
            # instead of bubbling MediaNotFoundError from get_item_by_uri.
            try:
                uri_media_type, _, _ = await parse_uri(item)
            except InvalidProviderURI, InvalidProviderID:
                uri_media_type = None
            if uri_media_type == MediaType.AUDIO_SOURCE:
                raise UnsupportedFeaturedException("AudioSource items can not be favorites")
            # a favorite URI always resolves to a media item, never a BrowseFolder
            item = cast("MediaItemType", await self.get_item_by_uri(item))
        if item.media_type == MediaType.AUDIO_SOURCE:
            # AudioSources are dynamic plugin surfaces (existence depends on a
            # running plugin and its current device state) and have no stable
            # library identity, so they can not be persisted as favorites.
            raise UnsupportedFeaturedException("AudioSource items can not be favorites")
        # make sure we have a full library item
        # a favorite must always be in the library
        full_item = cast(
            "MediaItemType",
            await self.get_item(
                item.media_type,
                item.item_id,
                item.provider,
            ),
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
            provider = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if not provider or not self.library_favorites_edit_supported(
                provider, full_item.media_type
            ):
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
            provider = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if not provider or not self.library_favorites_edit_supported(
                provider, full_item.media_type
            ):
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
            provider = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if not provider or not self.library_edit_supported(provider, full_item.media_type):
                continue
            if not self.library_sync_back_enabled(provider, full_item.media_type):
                continue
            prov_mapping.in_library = False
            self.mass.create_task(provider.library_remove(prov_mapping.item_id, media_type))
        # remove from library
        await ctrl.remove_item_from_library(library_item_id, recursive)

    @api_command("music/library/add_item")
    async def add_item_to_library(
        self, item: str | MediaItemType | ItemMapping, overwrite_existing: bool = False
    ) -> MediaItemType:
        """Add item (uri or mediaitem) to the library."""
        if isinstance(item, ItemMapping):
            # handle browse results that are returned as ItemMappings
            # uri is always populated post-init, so it is never None here
            item = cast("str", item.uri)
        # ensure we have a full item
        if isinstance(item, str):
            # Inspect the URI's media_type first so a stale audio-source URI
            # whose plugin is unloaded gives the honest rejection error
            # instead of bubbling MediaNotFoundError from get_item_by_uri.
            # Mirrors the same guard in add_item_to_favorites.
            try:
                uri_media_type, _, _ = await parse_uri(item)
            except InvalidProviderURI, InvalidProviderID:
                uri_media_type = None
            if uri_media_type == MediaType.AUDIO_SOURCE:
                raise UnsupportedFeaturedException("AudioSource items can not be library items")
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
        full_item = cast("MediaItemType", full_item)
        if full_item.media_type == MediaType.AUDIO_SOURCE:
            # AudioSources are dynamic plugin surfaces (existence depends on a
            # running plugin and its current device state) and have no stable
            # library identity, so they can not be persisted as library items.
            raise UnsupportedFeaturedException("AudioSource items can not be library items")
        # add to provider(s) library first
        for prov_mapping in full_item.provider_mappings:
            # we optimistically set in library to True to prevent items
            # from disappearing when the provider doesn't support library edit
            # or 2-way sync is disabled.
            prov_mapping.in_library = True
            provider = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if not provider or not self.library_edit_supported(provider, full_item.media_type):
                continue
            if not self.library_sync_back_enabled(provider, full_item.media_type):
                continue
            prov_item = deepcopy(full_item) if full_item.provider == "library" else full_item
            prov_item.provider = prov_mapping.provider_instance
            prov_item.item_id = prov_mapping.item_id
            self.mass.create_task(provider.library_add(prov_item))
        # add (or overwrite) to library
        ctrl = self.get_controller(full_item.media_type)
        # ctrl is chosen by media_type, so it matches full_item's runtime type
        library_item = await cast("MediaControllerBase[MediaItemType]", ctrl).add_item_to_library(
            full_item, overwrite_existing
        )
        # optionally import all album tracks into the library, mirroring the behavior
        # of the library sync (which only triggers on a (scheduled) full sync run)
        if full_item.media_type == MediaType.ALBUM:
            self._import_album_tracks_if_enabled(cast("Album", library_item))
        # perform full metadata scan
        await self.mass.metadata.update_metadata(library_item, overwrite_existing)
        return library_item

    def _import_album_tracks_if_enabled(self, album: Album) -> None:
        """Import all album tracks into the library for providers that have this enabled."""
        for prov_mapping in album.provider_mappings:
            # only consider mappings the album was actually added on; additional
            # mappings auto-created for other instances of the same provider
            # (via match_provider_instances) carry in_library=None and must be skipped
            if not prov_mapping.in_library:
                continue
            provider = self.mass.get_provider(prov_mapping.provider_instance)
            if not isinstance(provider, MusicProvider):
                continue
            if not provider.library_sync_album_tracks_enabled():
                continue
            self.mass.create_task(provider.import_album_tracks(prov_mapping.item_id, album.name))

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
            # a refresh URI always resolves to a media item, never a BrowseFolder
            media_item = cast("MediaItemType", await self.get_item_by_uri(media_item))

        media_type = media_item.media_type
        ctrl = self.get_controller(media_type)

        # genres are library-only items with no provider mappings, nothing to refresh
        if media_type == MediaType.GENRE:
            return media_item

        library_id = media_item.item_id if media_item.provider == "library" else None

        # cache in_library state before the provider fetch overwrites media_item
        in_library_cache: dict[tuple[str, str], bool] = {}
        for m in media_item.provider_mappings:
            if m.in_library is not None:
                in_library_cache[(m.provider_instance, m.item_id)] = m.in_library

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
            result: Sequence[MediaItemType | ItemMapping]
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
        # restore in_library state from before the refresh
        for prov_mapping in media_item.provider_mappings:
            key = (prov_mapping.provider_instance, prov_mapping.item_id)
            if prov_mapping.in_library is None and key in in_library_cache:
                prov_mapping.in_library = in_library_cache[key]
        # ctrl is chosen by media_type, so it matches media_item's runtime type
        library_item = await cast(
            "MediaControllerBase[MediaItemType]", ctrl
        ).update_item_in_library(library_id, media_item, overwrite=True)
        if library_item.media_type == MediaType.ALBUM:
            # update (local) album tracks
            for album_track in await self.albums.tracks(
                library_item.item_id, library_item.provider, True
            ):
                for prov_mapping in album_track.provider_mappings:
                    if not (prov := self.mass.get_provider(prov_mapping.provider_instance)):
                        continue
                    if not isinstance(prov, MusicProvider):
                        continue
                    if prov.is_streaming_provider:
                        continue
                    with suppress(MediaNotFoundError):
                        prov_track = await prov.get_track(prov_mapping.item_id)
                        await self.mass.music.tracks.update_item_in_library(
                            album_track.item_id, prov_track
                        )
        await cast("MediaControllerBase[MediaItemType]", ctrl).match_providers(library_item)
        await self.mass.metadata.update_metadata(library_item, force_refresh=True)
        return library_item

    @api_command("music/mark_played")
    async def mark_item_played(
        self,
        media_item: MediaItemType,
        fully_played: bool = True,
        seconds_played: int | None = None,
        is_playing: bool = False,
        userid: str | None = None,
        queue_id: str | None = None,
        user_initiated: bool = True,
        skip_artist_ids: list[str] | None = None,
    ) -> None:
        """
        Mark item as played in playlog.

        :param media_item: The media item to mark as played.
        :param fully_played: If True, mark the item as fully played.
        :param seconds_played: The number of seconds played.
        :param is_playing: If True, the item is currently playing.
        :param userid: The user ID to mark the item as played for (instead of the current user).
        :param queue_id: The queue ID where the item was played.
        :param user_initiated: If True, the playback was initiated by the user (e.g. enqueued).
        :param skip_artist_ids: Library artist ids to skip when crediting an album's artists.
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
            "queue_id": queue_id,
            "user_initiated": user_initiated,
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

        # update generic playlog table (when not playing)
        if not is_playing:
            if user:
                user_ids = [user.user_id]
            else:
                # NOTE: if no user was found, we will alter the playlog for all users
                user_ids = [user.user_id for user in await self.mass.webserver.auth.list_users()]
            for user_id in user_ids:
                params["userid"] = user_id
                await self.database.insert(
                    DB_TABLE_PLAYLOG,
                    params,
                    allow_replace=True,
                )

        # Set seconds_played in accordance with fully_played, if the media_item has
        # a duration, before it is forwarded to music_providers
        if seconds_played is None:
            seconds_played = 0
            if (
                fully_played
                and not isinstance(media_item, Album | Artist | Genre | Playlist | Podcast)
                and isinstance(media_item.duration, int)  # for Radio duration can be None
            ):
                seconds_played = media_item.duration

        # forward to provider(s) to sync resume state (e.g. for audiobooks)
        for prov_mapping in media_item.provider_mappings:
            if (
                user
                and user.provider_filter
                and prov_mapping.provider_instance not in user.provider_filter
            ):
                continue
            if music_prov := self.mass.get_provider(prov_mapping.provider_instance):
                if music_prov.type != ProviderType.MUSIC:
                    continue
                music_prov = cast("MusicProvider", music_prov)
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
        try:
            ctrl = self.get_controller(media_item.media_type)
        except NotImplementedError:
            # skip non-library media types (e.g. AudioSource plugin sources)
            return
        db_item = await ctrl.get_library_item_by_prov_id(media_item.item_id, media_item.provider)
        if db_item:
            await self.database.execute(
                f"UPDATE {ctrl.db_table} SET play_count = play_count + 1, "
                f"last_played = {timestamp} WHERE item_id = {db_item.item_id}"
            )
            if isinstance(media_item, Track):
                self.logger.debug("Credited play for track '%s'", media_item.name)
        if isinstance(media_item, Track | Album):
            await self._credit_artist_plays(
                media_item.artists,
                timestamp=timestamp,
                user_ids=user_ids,
                queue_id=queue_id,
                skip_ids=set(skip_artist_ids or ()),
            )
        await self.database.commit()

    async def resolve_library_artist_ids(self, artists: Iterable[Artist | ItemMapping]) -> set[str]:
        """Resolve the given artist references to their library item ids (when present)."""
        ids: set[str] = set()
        for artist in artists:
            db_artist = await self.artists.get_library_item_by_prov_id(
                artist.item_id, artist.provider
            )
            if db_artist is not None:
                ids.add(db_artist.item_id)
        return ids

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
            user_ids = [user.user_id]
        else:
            # NOTE: if no user was found, we will alter the playlog for all users
            user_ids = [user.user_id for user in await self.mass.webserver.auth.list_users()]
        for user_id in user_ids:
            params["userid"] = user_id
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
                if music_prov.type != ProviderType.MUSIC:
                    continue
                music_prov = cast("MusicProvider", music_prov)
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
                if not allow_item_mapping and not isinstance(search_track, Track):
                    continue
                if not compare_strings(track_name, search_track.name):
                    continue
                if not compare_version(version, search_track.version):
                    continue
                # check optional artist(s)
                if artist_name and isinstance(search_track, Track):
                    for artist in search_track.artists:
                        if compare_strings(artist_name, artist.name, False):
                            break
                    else:
                        # no artist match found: abort
                        continue
                # check optional album
                if album_name and isinstance(search_track, Track):
                    track_album = search_track.album
                    # a track without album info can never match a requested album
                    if track_album is None or not compare_strings(
                        album_name, track_album.name, False
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
            for single_artist in artists:
                return await self.get_track_by_name(
                    track_name=track_name,
                    artist_name=single_artist.split(splitter)[0].strip(),
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
        provider_timestamp: datetime | None = None

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

        provider_instances = {x.provider_instance for x in media_item.provider_mappings}
        if user and user.provider_filter:
            # only if the user has provider filters configured
            # otherwise we allow all providers
            preferred_provider_instances = provider_instances.intersection(user.provider_filter)
        else:
            preferred_provider_instances = provider_instances

        preferred_providers = [
            x
            for x in media_item.provider_mappings
            if x.provider_instance in preferred_provider_instances
        ]

        # Try to get position from providers
        for prov_mapping in preferred_providers:
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
                    provider_timestamp,
                ) = await provider.get_resume_position(prov_mapping.item_id, media_item.media_type)
                break  # Use first provider that returns data

        # Get MA's internal position from playlog
        ma_fully_played = False
        ma_position_ms = 0
        ma_timestamp = from_utc_timestamp(0)
        params = {
            "media_type": media_item.media_type.value,
            "item_id": media_item.item_id,
            "provider": media_item.provider,
        }
        if userid:
            params["userid"] = userid
        elif user:
            params["userid"] = user.user_id
        if db_entry := await self.database.get_row(DB_TABLE_PLAYLOG, params):
            ma_position_ms = db_entry["seconds_played"] * 1000 if db_entry["seconds_played"] else 0
            # fully_played is a nullable column; treat an unknown (NULL) value as not played
            ma_fully_played = parse_optional_bool(db_entry["fully_played"]) or False
            ma_timestamp = from_utc_timestamp(db_entry["timestamp"])

        if provider_timestamp is not None and provider_timestamp > ma_timestamp:
            return provider_fully_played, provider_position_ms
        # Return the higher position to ensure users never lose progress
        if ma_position_ms >= provider_position_ms:
            return ma_fully_played, ma_position_ms
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
        # suppress the per-item MEDIA_ITEM_UPDATED events during this bulk removal so we
        # don't flood subscribers; they refresh once via the PROVIDERS_UPDATED event
        token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
        try:
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
                    "WHERE media_type = :media_type "
                    "AND provider_instance = :provider_instance"
                )
                params = {
                    "media_type": ctrl.media_type.value,
                    "provider_instance": provider_instance,
                }
                for db_row in await self.database.get_rows_from_query(query, params, limit=100000):
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
        finally:
            SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)

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
        if not (
            provider := self.mass.get_provider(provider_instance_id, provider_type=MusicProvider)
        ):
            return
        self.unschedule_provider_sync(provider.instance_id, clear_persisted_state=False)
        for media_type in MediaType:
            if not self.library_supported(provider, media_type):
                continue
            await self._schedule_provider_mediatype_sync(provider, media_type, True)

    def unschedule_provider_sync(
        self, provider_instance_id: str, clear_persisted_state: bool = True
    ) -> None:
        """Unschedule Library sync for given provider.

        :param provider_instance_id: The provider instance id to unschedule.
        :param clear_persisted_state: Whether to remove persisted schedule state from config.
        """
        for media_type in MediaType:
            self.mass.tasks.unregister_scheduled_task(
                self._get_sync_task_id(provider_instance_id, media_type),
                clear_persisted_state=clear_persisted_state,
            )

    def get_provider_sync_schedule(
        self, provider_instance_id: str, media_type: MediaType
    ) -> TaskSchedule | None:
        """Return the effective schedule for a provider sync task, if any."""
        task_id = self._get_sync_task_id(provider_instance_id, media_type)
        with suppress(InvalidDataError):
            task = self.mass.tasks.get_task(task_id)
            return task.schedule
        if not (
            provider := self.mass.get_provider(provider_instance_id, provider_type=MusicProvider)
        ):
            return None
        if not self.library_supported(provider, media_type):
            return None
        return provider.get_default_library_sync_schedule(media_type)

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
            if not isinstance(provider, MusicProvider):
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
        # ctrl is chosen by media_type, so it matches db_item's runtime type
        await cast("MediaControllerBase[MediaItemType]", ctrl).match_providers(db_item)

    async def update_provider_mapping(
        self,
        media_type: MediaType,
        db_id: str | int,
        provider_instance_id: str,
        provider_item_id: str,
        *,
        available: bool | Any = UNSET,
        in_library: bool | Any = UNSET,
        is_unique: bool | None | Any = UNSET,
        url: str | None | Any = UNSET,
        details: str | None | Any = UNSET,
        audio_format: AudioFormat | Any = UNSET,
    ) -> None:
        """Update an existing provider mapping for a library item."""
        ctrl = self.get_controller(media_type)
        await ctrl.update_provider_mapping(
            item_id=db_id,
            provider_instance_id=provider_instance_id,
            provider_item_id=provider_item_id,
            available=available,
            in_library=in_library,
            is_unique=is_unique,
            url=url,
            details=details,
            audio_format=audio_format,
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
                items=cast(
                    "UniqueList[MediaItemType | ItemMapping | BrowseFolder]",
                    await self.in_progress_items(limit=10),
                ),
            ),
            RecommendationFolder(
                item_id="recently_played",
                provider="library",
                name="Recently played",
                translation_key="recently_played",
                icon="mdi-motion-play",
                items=cast(
                    "UniqueList[MediaItemType | ItemMapping | BrowseFolder]",
                    await self.recently_played(limit=10, user_initiated_only=False),
                ),
            ),
            RecommendationFolder(
                item_id="recently_added_tracks",
                provider="library",
                name="Recently added tracks",
                translation_key="recently_added_tracks",
                icon="music-note-plus",
                items=cast(
                    "UniqueList[MediaItemType | ItemMapping | BrowseFolder]",
                    await self.tracks.library_items(limit=10, order_by="timestamp_added_desc"),
                ),
            ),
            RecommendationFolder(
                item_id="recently_added_albums",
                provider="library",
                name="Recently added albums",
                translation_key="recently_added_albums",
                icon="music-note-plus",
                items=cast(
                    "UniqueList[MediaItemType | ItemMapping | BrowseFolder]",
                    await self.albums.library_items(limit=10, order_by="timestamp_added_desc"),
                ),
            ),
            RecommendationFolder(
                item_id="random_artists",
                provider="library",
                name="Random artists",
                translation_key="random_artists",
                icon="mdi-account-music",
                items=cast(
                    "UniqueList[MediaItemType | ItemMapping | BrowseFolder]",
                    await self.artists.library_items(limit=10, order_by="random_play_count"),
                ),
            ),
            RecommendationFolder(
                item_id="random_albums",
                provider="library",
                name="Random albums",
                translation_key="random_albums",
                icon="mdi-album",
                items=cast(
                    "UniqueList[MediaItemType | ItemMapping | BrowseFolder]",
                    await self.albums.library_items(limit=10, order_by="random_play_count"),
                ),
            ),
            RecommendationFolder(
                item_id="recent_favorite_tracks",
                provider="library",
                name="Recently favorited tracks",
                translation_key="recent_favorite_tracks",
                icon="mdi-file-music",
                items=cast(
                    "UniqueList[MediaItemType | ItemMapping | BrowseFolder]",
                    await self.tracks.library_items(
                        favorite=True, limit=10, order_by="timestamp_modified_desc"
                    ),
                ),
            ),
            RecommendationFolder(
                item_id="favorite_playlists",
                provider="library",
                name="Favorite playlists",
                translation_key="favorite_playlists",
                icon="mdi-playlist-music",
                items=cast(
                    "UniqueList[MediaItemType | ItemMapping | BrowseFolder]",
                    await self.playlists.library_items(favorite=True, limit=10, order_by="random"),
                ),
            ),
            RecommendationFolder(
                item_id="favorite_radio",
                provider="library",
                name="Favorite Radio stations",
                translation_key="favorite_radio_stations",
                icon="mdi-access-point",
                items=cast(
                    "UniqueList[MediaItemType | ItemMapping | BrowseFolder]",
                    await self.radio.library_items(
                        favorite=True, limit=10, order_by="play_count_desc"
                    ),
                ),
            ),
        ]

    async def _get_provider_recommendations(
        self, provider: MusicProvider | MetadataProvider | PluginProvider
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

    def _create_provider_sync_handler(
        self, provider: MusicProvider, media_type: MediaType
    ) -> Callable[[], Awaitable[None]]:
        """Create the coroutine used for a managed provider sync task."""

        async def run_sync() -> None:
            try:
                async with self._sync_lock:
                    await provider.sync_library(media_type)
            finally:
                self.mass.call_later(
                    0,
                    self._handle_sync_completion_check,
                    task_id=MUSIC_SYNC_COMPLETION_CHECK_TASK_ID,
                )

        return run_sync

    def _get_sync_task_id(self, provider: MusicProvider | str, media_type: MediaType) -> str:
        """Return deterministic task id for a provider sync."""
        provider_instance = (
            provider.instance_id if isinstance(provider, MusicProvider) else provider
        )
        return f"music_sync_{provider_instance}_{media_type.value}"

    def _get_sync_task_name(self, provider: MusicProvider, media_type: MediaType) -> str:
        """Return display name for a provider sync task."""
        return f"Sync {provider.name} {media_type.value}s"

    def _get_sync_task_translation_key(self, media_type: MediaType) -> str:
        """Return translation key for a provider sync task."""
        if media_type == MediaType.ARTIST:
            return "background_task.sync_provider_artists"
        if media_type == MediaType.ALBUM:
            return "background_task.sync_provider_albums"
        if media_type == MediaType.TRACK:
            return "background_task.sync_provider_tracks"
        if media_type == MediaType.PLAYLIST:
            return "background_task.sync_provider_playlists"
        if media_type == MediaType.RADIO:
            return "background_task.sync_provider_radios"
        if media_type == MediaType.AUDIOBOOK:
            return "background_task.sync_provider_audiobooks"
        if media_type == MediaType.PODCAST:
            return "background_task.sync_provider_podcasts"
        return "settings.sync"

    def _get_sync_task_metadata(
        self, provider: MusicProvider, media_type: MediaType
    ) -> TaskMetadata:
        """Return metadata for a provider sync task."""
        return {
            "task_domain": "music_sync",
            "provider_domain": provider.domain,
            "provider_instance": provider.instance_id,
            "provider_name": provider.name,
            "media_type": media_type.value,
        }

    def _handle_sync_completion_check(self) -> None:
        """Run follow-up maintenance when no provider sync tasks remain active."""
        if self.active_sync_tasks:
            return
        self.mass.signal_event(EventType.MUSIC_SYNC_COMPLETED)
        self._queue_database_cleanup_task()

    def _register_database_cleanup_task(self) -> BackgroundTask:
        """Register the recurring database cleanup background task."""
        utc_hour, utc_minute = local_clock_time_to_utc(5, 0)
        desired_schedule = TaskSchedule.daily(hour=utc_hour, minute=utc_minute)
        return self.mass.tasks.register_scheduled_task(
            task_id=DATABASE_CLEANUP_TASK_ID,
            name="Database cleanup",
            handler=self._cleanup_database,
            schedule=desired_schedule,
            translation_key="background_task.database_cleanup",
            metadata={
                "task_domain": "music_database_cleanup",
            },
            allow_retry=True,
        )

    def _register_provider_mapping_correction_task(self) -> BackgroundTask:
        """Register the recurring provider mapping correction background task."""
        utc_hour, utc_minute = local_clock_time_to_utc(4, 0)
        desired_schedule = TaskSchedule.daily(every=30, hour=utc_hour, minute=utc_minute)
        return self.mass.tasks.register_scheduled_task(
            task_id=PROVIDER_MAPPING_CORRECTION_TASK_ID,
            name="Correct provider mappings",
            handler=self.correct_multi_instance_provider_mappings,
            schedule=desired_schedule,
            translation_key="background_task.correct_provider_mappings",
            metadata={
                "task_domain": "music_provider_mapping_correction",
            },
            allow_retry=True,
        )

    def _queue_database_cleanup_task(self) -> BackgroundTask:
        """Queue the post-sync database cleanup as a managed background task."""
        self._register_database_cleanup_task()
        return self.mass.tasks.run_task(DATABASE_CLEANUP_TASK_ID)

    def queue_provider_mapping_correction_task(self) -> BackgroundTask:
        """Queue the provider mapping correction as a managed background task."""
        self._register_provider_mapping_correction_task()
        return self.mass.tasks.run_task(PROVIDER_MAPPING_CORRECTION_TASK_ID)

    def _sort_search_result[SortItemT: MediaItemType | ItemMapping](
        self,
        search_query: str,
        items: Sequence[SortItemT],
    ) -> UniqueList[SortItemT]:
        """Sort search results on priority/preference."""
        scored_items: list[tuple[int, SortItemT]] = []
        # search results are already sorted by (streaming) providers on relevance
        # but we prefer exact name matches and library items so we simply put those
        # on top of the list.
        safe_title_str = create_safe_string(search_query)
        if " - " in search_query:
            artist_name, title_alt = search_query.split(" - ", 1)
            safe_title_alt = create_safe_string(title_alt)
            safe_artist_str = create_safe_string(artist_name)
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
        # handle mediatype specific sync config
        conf_key = f"library_sync_{media_type}s"
        sync_conf: ConfigValueType = await self.mass.config.get_provider_config_value(
            provider.instance_id, conf_key
        )
        if not sync_conf:
            self.mass.tasks.unregister_scheduled_task(self._get_sync_task_id(provider, media_type))
            return
        self.mass.tasks.register_scheduled_task(
            task_id=self._get_sync_task_id(provider, media_type),
            name=self._get_sync_task_name(provider, media_type),
            handler=self._create_provider_sync_handler(provider, media_type),
            schedule=provider.get_default_library_sync_schedule(media_type),
            initial_delay=10 if is_initial else None,
            translation_key=self._get_sync_task_translation_key(media_type),
            translation_args=[provider.name],
            metadata=self._get_sync_task_metadata(provider, media_type),
            allow_retry=True,
        )

    async def _cleanup_database(self) -> None:
        """Perform database cleanup/maintenance."""
        self.logger.debug("Performing database cleanup...")
        update_current_task_progress_text("Cleaning old playlog entries")
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
            update_current_task_progress_text(f"Cleaning {ctrl.media_type.value} library records")
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
        update_current_task_progress_text("Database cleanup finished")
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
        except KeyError, ValueError:
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
                prev_version = 0

        # store current schema version
        await self._database.insert_or_replace(
            DB_TABLE_SETTINGS,
            {"key": "version", "value": str(DB_SCHEMA_VERSION), "type": "str"},
        )
        # create indexes and triggers if needed
        await self.__create_database_indexes()
        await self.__create_database_triggers()
        if prev_version == 0:
            # fresh install - populate default genres
            await self.genres.restore_default_genres()
        # compact db - skip the full rebuild unless a meaningful share is reclaimable
        try:
            reclaimable_ratio = await self._database.get_reclaimable_ratio()
            if reclaimable_ratio < VACUUM_MIN_RECLAIM_RATIO:
                self.logger.debug(
                    "Skipping database compaction (only %.1f%% reclaimable)",
                    reclaimable_ratio * 100,
                )
            else:
                self.logger.debug(
                    "Compacting database (%.1f%% reclaimable)...", reclaimable_ratio * 100
                )
                await self._database.vacuum()
                self.logger.debug("Compacting database done")
        except Exception as err:
            self.logger.warning("Database vacuum failed: %s", str(err))

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
                    except KeyError, ValueError:
                        # this is not a valid date, so we set it to None
                        metadata["release_date"] = None
                        await self.database.update(
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
                await self.database.execute(f"DROP TRIGGER IF EXISTS update_{db_table}_timestamp;")

        if prev_version <= 18:
            # add in_library column to provider_mappings table
            await self.database.execute(
                f"ALTER TABLE {DB_TABLE_PROVIDER_MAPPINGS} ADD COLUMN in_library "
                "BOOLEAN NOT NULL DEFAULT 0;"
            )
            # migrate existing entries in provider_mappings which are filesystem
            await self.database.execute(
                f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
                "WHERE provider_domain in ('filesystem_local', 'filesystem_smb');"
            )

        if prev_version <= 20:
            # drop column cache_checksum from playlists table
            # this is no longer used and is a leftover from previous designs
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLISTS} DROP COLUMN cache_checksum"
                )
            except Exception as err:
                if "no such column" not in str(err):
                    raise

        if prev_version <= 21:
            # drop table for smart fades analysis - it will be recreated with needed columns
            await self.database.execute("DROP TABLE IF EXISTS smart_fades_analysis")
            await self.__create_database_tables()

        if prev_version <= 22:
            # add userid column to playlog table
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN userid TEXT"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise
            # Note: SQLite doesn't support modifying constraints directly
            # The UNIQUE constraint will be updated when the table is recreated
            # For now, we'll keep the old constraint and add a new one via unique index
            try:
                await self.database.execute(f"DROP INDEX IF EXISTS {DB_TABLE_PLAYLOG}_unique_idx")
                await self.database.execute(
                    f"CREATE UNIQUE INDEX {DB_TABLE_PLAYLOG}_unique_idx "
                    f"ON {DB_TABLE_PLAYLOG}(item_id,provider,media_type,userid)"
                )
            except Exception as err:
                # If we can't create the index due to duplicate entries, log and continue
                self.logger.warning("Could not create unique index on playlog: %s", err)

        if prev_version <= 23:
            # add is_unique column to provider_mappings table
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_PROVIDER_MAPPINGS} ADD COLUMN is_unique BOOLEAN"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise

        if prev_version <= 24:
            # add queue_id and user_initiated columns to playlog table
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLOG} ADD COLUMN queue_id TEXT"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLOG} "
                    "ADD COLUMN user_initiated BOOLEAN NOT NULL DEFAULT 1"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise

        if prev_version <= 26:
            # force in_library=True for provider mappings from non-streaming providers
            # streaming providers will be automatically added to library when synced
            await self.database.execute(
                f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
                "WHERE provider_domain NOT IN "
                "('spotify', 'deezer', 'tidal', 'qobuz', 'apple_music', 'ytmusic');"
            )
            # also set in_library=True for all radio items
            await self.database.execute(
                f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
                "WHERE media_type = 'radio';"
            )
            # remove invalid playlist provider mappings for playlists which are not in library
            await self.database.execute(
                f"DELETE FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                "WHERE media_type = 'playlist' AND in_library = 0;"
            )

        if prev_version <= 27:
            # set streaming provider mappings to in_library=True, but only for items
            # that do not already have any mapping with in_library=True
            # (to avoid overwriting explicit values in multi-instance setups)
            await self.database.execute(
                f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
                "WHERE provider_domain NOT IN "
                "('filesystem_local', 'builtin', 'test', 'jellyfin', 'emby', "
                "'plex', 'opensubsonic', 'audiobookshelf', 'gpodder', 'podcastfeed') "
                "AND NOT EXISTS ("
                f"SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} AS pm2 "
                f"WHERE pm2.media_type = {DB_TABLE_PROVIDER_MAPPINGS}.media_type "
                f"AND pm2.item_id = {DB_TABLE_PROVIDER_MAPPINGS}.item_id "
                "AND pm2.in_library = 1)"
            )

        if prev_version <= 28:
            # create genre/alias tables
            await self.__create_database_tables()

            # Use raw aiosqlite connection for bulk operations.
            db = self.database._db

            empty_metadata = serialize_to_json({})
            empty_external_ids = serialize_to_json(set())

            def _normalize_name(raw_name: str) -> tuple[str, str, str, str]:
                name = raw_name.strip()
                sort_name = name
                search_name = create_safe_string(name, True, True)
                search_sort_name = create_safe_string(sort_name or "", True, True)
                return name, sort_name, search_name, search_sort_name

            genre_cache: dict[str, int] = {}

            genre_insert_sql = (
                f"INSERT OR IGNORE INTO {DB_TABLE_GENRES}"
                "(name, sort_name, translation_key, description, favorite, "
                "metadata, external_ids, genre_aliases, play_count, last_played, "
                "search_name, search_sort_name) "
                "VALUES (?, ?, ?, NULL, 0, ?, ?, ?, 0, 0, ?, ?)"
            )
            genre_select_sql = f"SELECT item_id FROM {DB_TABLE_GENRES} WHERE search_name = ?"

            async def _get_or_create_genre(
                raw_name: str,
                aliases: list[str] | None = None,
                translation_key: str | None = None,
            ) -> int:
                name, sort_name, search_name, search_sort_name = _normalize_name(raw_name)
                if not search_name:
                    return 0
                if search_name in genre_cache:
                    return genre_cache[search_name]
                aliases_json = serialize_to_json(aliases or [name])
                icon_metadata = GenreController._get_genre_icon_metadata(translation_key)
                metadata_json = (
                    serialize_to_json(icon_metadata.to_dict()) if icon_metadata else empty_metadata
                )
                row_id = await db.execute_insert(
                    genre_insert_sql,
                    (
                        name,
                        sort_name,
                        translation_key,
                        metadata_json,
                        empty_external_ids,
                        aliases_json,
                        search_name,
                        search_sort_name,
                    ),
                )
                if row_id and row_id[0]:
                    genre_cache[search_name] = row_id[0]
                    return cast("int", row_id[0])
                async with db.execute(genre_select_sql, (search_name,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        genre_cache[search_name] = row[0]
                        return cast("int", row[0])
                return 0

            # Phase 1: Seed DEFAULT_GENRE_MAPPING — create genres with aliases.
            # Build n:n lookup: normalized alias name -> list of genre_ids.
            # One alias can belong to multiple genres (e.g. "funk" is both
            # a standalone genre and an alias of Soul/R&B).
            alias_to_genre: dict[str, list[int]] = {}
            for entry in DEFAULT_GENRE_MAPPING:
                genre_name = entry.get("genre")
                if not genre_name:
                    continue
                all_aliases = [genre_name, *entry.get("aliases", [])]
                genre_id = await _get_or_create_genre(
                    genre_name,
                    aliases=all_aliases,
                    translation_key=entry.get("translation_key"),
                )
                if not genre_id:
                    continue
                for alias in all_aliases:
                    norm = create_safe_string(alias.strip(), True, True)
                    if norm:
                        alias_to_genre.setdefault(norm, [])
                        if genre_id not in alias_to_genre[norm]:
                            alias_to_genre[norm].append(genre_id)
            await db.commit()

            # Phase 2: Discover unique genre names from all media items,
            # create genres for unknown names, then bulk-insert mappings.
            media_tables = (
                (DB_TABLE_TRACKS, MediaType.TRACK),
                (DB_TABLE_ALBUMS, MediaType.ALBUM),
                (DB_TABLE_ARTISTS, MediaType.ARTIST),
                (DB_TABLE_PLAYLISTS, MediaType.PLAYLIST),
                (DB_TABLE_RADIOS, MediaType.RADIO),
                (DB_TABLE_AUDIOBOOKS, MediaType.AUDIOBOOK),
                (DB_TABLE_PODCASTS, MediaType.PODCAST),
            )

            # 2a: Extract all unique raw genre names from metadata
            union_parts = [
                f"SELECT DISTINCT TRIM(g.value) AS raw_name "
                f"FROM {table}, json_each(json_extract({table}.metadata, '$.genres')) AS g "
                f"WHERE json_extract({table}.metadata, '$.genres') IS NOT NULL "
                f"AND json_extract({table}.metadata, '$.genres') != '[]'"
                for table, _ in media_tables
            ]
            unique_names_sql = " UNION ".join(union_parts)
            self.logger.info("Genre migration - unique names query:\n%s", unique_names_sql)
            async with db.execute(unique_names_sql) as cursor:
                unique_raw_names = [row[0] for row in await cursor.fetchall() if row[0]]
            self.logger.info(
                "Genre migration - discovered %d unique genre names", len(unique_raw_names)
            )

            # 2b: Ensure genres exist for all discovered names.
            # Names already covered by Phase 1 aliases just reuse those genre(s).
            # New names get their own genre. One alias can map to multiple genres (n:n).
            raw_name_to_genres: dict[str, list[int]] = {}
            for raw_name in unique_raw_names:
                norm = create_safe_string(raw_name.strip(), True, True)
                if not norm:
                    continue
                if norm in alias_to_genre:
                    raw_name_to_genres[raw_name] = list(alias_to_genre[norm])
                    self.logger.debug(
                        "Genre migration - resolved %r -> genre_ids %s (alias match)",
                        raw_name,
                        alias_to_genre[norm],
                    )
                else:
                    genre_id = await _get_or_create_genre(raw_name)
                    if genre_id:
                        raw_name_to_genres[raw_name] = [genre_id]
                        alias_to_genre[norm] = [genre_id]
                        self.logger.debug(
                            "Genre migration - resolved %r -> genre_id %d (new genre)",
                            raw_name,
                            genre_id,
                        )
            await db.commit()
            self.logger.info(
                "Genre migration - resolved %d unique genre names", len(raw_name_to_genres)
            )

            # 2c: Add discovered raw names as aliases to their resolved genres
            # so that frontend searches by raw name find the parent genre.
            genre_new_aliases: dict[int, list[str]] = {}
            for raw_name, gids in raw_name_to_genres.items():
                for gid in gids:
                    genre_new_aliases.setdefault(gid, []).append(raw_name)
            for gid, new_aliases in genre_new_aliases.items():
                async with db.execute(
                    f"SELECT genre_aliases FROM {DB_TABLE_GENRES} WHERE item_id = :gid",
                    {"gid": gid},
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    continue
                existing = json_loads(row[0]) if row[0] else []
                existing_norms = {create_safe_string(a, True, True) for a in existing}
                to_add = [
                    a
                    for a in new_aliases
                    if create_safe_string(a, True, True) not in existing_norms
                ]
                if to_add:
                    merged = existing + to_add
                    await db.execute(
                        f"UPDATE {DB_TABLE_GENRES} SET genre_aliases = :aliases "
                        "WHERE item_id = :gid",
                        {"aliases": json_dumps(merged), "gid": gid},
                    )
            await db.commit()

            # 2d: Build CTE with (raw_name, genre_id) and do one INSERT per
            # media type using json_each to map media items directly to genres.
            # One raw_name can map to multiple genre_ids (n:n).
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
                    self.logger.info(
                        "Genre migration - %s query:\n%s", media_type.value, full_query
                    )
                    await db.execute(full_query)
                    await db.commit()

        if prev_version <= 29:
            # Smart fades analyses were previously computed on silence-stripped audio,
            # so beat timestamps are misaligned with the unstripped buffers now passed
            # to the crossfade mixer. Truncate the table so all analyses are re-computed.
            with suppress(Exception):
                await self.database.execute("DELETE FROM smart_fades_analysis")

        if prev_version <= 30:
            # add supported_mediatypes column to playlist table, and make {MediaType.TRACK},
            # i.e. ["track"] the default, as this was the only media type supported.
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLISTS} ADD COLUMN supported_mediatypes"
                    " json DEFAULT '[\"track\"]' NOT NULL"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise

        if prev_version <= 31:
            # create the genre_media_item_exclusion table (new in schema 31)
            await self.database.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(
                [genre_id] INTEGER NOT NULL,
                [media_id] INTEGER NOT NULL,
                [media_type] TEXT NOT NULL,
                FOREIGN KEY([genre_id]) REFERENCES [genres]([item_id]),
                UNIQUE(genre_id, media_id, media_type)
                );"""
            )
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}_media_idx "
                f"on {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(media_id,media_type);"
            )
            await self.database.execute(
                f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}_genre_idx "
                f"on {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(genre_id);"
            )

        if prev_version <= 32:
            # recreate genre_media_item_mapping with nullable alias and is_derived column
            # (new in schema 33 to support propagated genre mappings from tracks)
            await self.database.execute(
                f"ALTER TABLE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
                f"RENAME TO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_old;"
            )
            await self.database.execute(
                f"""
                CREATE TABLE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(
                [genre_id] INTEGER NOT NULL,
                [media_id] INTEGER NOT NULL,
                [media_type] TEXT NOT NULL,
                [alias] TEXT,
                [is_derived] BOOLEAN NOT NULL DEFAULT 0,
                FOREIGN KEY([genre_id]) REFERENCES [genres]([item_id]),
                UNIQUE(genre_id, media_id, media_type)
                );"""
            )
            await self.database.execute(
                f"INSERT INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
                f"(genre_id, media_id, media_type, alias) "
                f"SELECT genre_id, media_id, media_type, alias "
                f"FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_old;"
            )
            await self.database.execute(f"DROP TABLE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_old;")

        if prev_version <= 33:
            # add is_excluded column to genres table (new in schema 34)
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_GENRES} "
                    "ADD COLUMN [is_excluded] BOOLEAN NOT NULL DEFAULT 0;"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise
            # drop the old genre_global_exclusion table (replaced by is_excluded column)
            await self.database.execute("DROP TABLE IF EXISTS genre_global_exclusion;")
            # add is_default column to genres table (new in schema 34)
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_GENRES} "
                    "ADD COLUMN [is_default] BOOLEAN NOT NULL DEFAULT 0;"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise
            # mark all existing genres with a translation_key as default
            await self.database.execute(
                f"UPDATE {DB_TABLE_GENRES} SET is_default = 1 WHERE translation_key IS NOT NULL;"
            )
        if prev_version <= 34:
            # fix filesystem playlists missing in_library flag
            await self.database.execute(
                f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET in_library = 1 "
                "WHERE media_type = 'playlist' "
                "AND provider_domain IN ('filesystem_local', 'filesystem_smb', 'filesystem_nfs');"
            )

        if prev_version <= 35:
            # add is_dynamic column to playlist table
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLISTS} ADD COLUMN is_dynamic"
                    " BOOLEAN NOT NULL DEFAULT 0"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise
            # backfill is_dynamic for existing Apple Music station playlists
            await self.database.execute(
                f"UPDATE {DB_TABLE_PLAYLISTS} SET is_dynamic = 1 "
                f"WHERE item_id IN ("
                f"  SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} "
                f"  WHERE media_type = 'playlist' "
                f"  AND provider_domain = 'apple_music' "
                f"  AND provider_item_id LIKE 'ra.%'"
                f")"
            )

        if prev_version <= 36:
            # drop legacy smart_fades_analysis table — analysis is now handled by
            # audio analysis providers and stored in the audio_analysis table.
            await self.database.execute("DROP TABLE IF EXISTS smart_fades_analysis")

        if prev_version <= 37:
            # purge unreliable loudness measurements persisted by earlier versions
            # (ebur128 reports ~-70 LUFS on near-silence / early-cancelled streams,
            # which caused huge gain corrections on subsequent plays)
            await self.database.execute(
                f"DELETE FROM {DB_TABLE_LOUDNESS_MEASUREMENTS} "
                f"WHERE loudness <= {LOUDNESS_MEASUREMENT_MIN_LUFS}"
            )
            await self.database.execute(
                f"UPDATE {DB_TABLE_LOUDNESS_MEASUREMENTS} "
                f"SET loudness_album = NULL "
                f"WHERE loudness_album <= {LOUDNESS_MEASUREMENT_MIN_LUFS}"
            )

        if prev_version <= 38:
            # stable 2.8.9 shipped schema v38 without the smart_fades_analysis drop
            # (that drop is gated at <= 36, which v38 users leapfrog). re-run it here
            # so stable->2.9.0 upgraders also lose the legacy table. idempotent: a
            # no-op for beta users who already dropped it at v36.
            await self.database.execute("DROP TABLE IF EXISTS smart_fades_analysis")
            # migrate loudness measurements to the unified audio_analysis table
            # under the new builtin loudness_analysis provider, then drop the
            # legacy table. album loudness rides along when present.
            await self.database.execute(
                f"INSERT OR IGNORE INTO {DB_TABLE_AUDIO_ANALYSIS} "
                f"(media_type, item_id, provider, aa_provider_domain, "
                f" analysis_data, analysis_version) "
                f"SELECT media_type, item_id, provider, 'loudness_analysis', "
                f"       json_object("
                f"           'loudness_integrated', loudness, "
                f"           'loudness_album', loudness_album"
                f"       ), 1 "
                f"FROM {DB_TABLE_LOUDNESS_MEASUREMENTS} "
                f"WHERE loudness IS NOT NULL "
                f"  AND loudness > {LOUDNESS_MEASUREMENT_MIN_LUFS}"
            )
            await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_LOUDNESS_MEASUREMENTS}")

        if prev_version <= 39:
            # add is_manual column to genre_media_item_mapping
            try:
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} "
                    "ADD COLUMN [is_manual] BOOLEAN NOT NULL DEFAULT 0;"
                )
            except Exception as err:
                if "duplicate column" not in str(err):
                    raise

        if prev_version <= 40:
            # genre icons were previously stored with an absolute filesystem path to
            # the builtin SVG, which is install-location dependent. after a runtime
            # upgrade or relocation (e.g. the python3.13 -> python3.14 site-packages
            # move) that path no longer existed, so genre icons 404'd via imageproxy.
            # rewrite them to the install-independent "<GENRE_ICONS_DIR_NAME>/<file>"
            # form; the builtin provider resolves that against RESOURCES_DIR at serve
            # time.
            genre_dir_marker = f"/resources/{GENRE_ICONS_DIR_NAME}/"
            async for db_row in self.database.iter_items(DB_TABLE_GENRES):
                raw_metadata = db_row["metadata"]
                if not raw_metadata:
                    continue
                metadata = json_loads(raw_metadata)
                images = metadata.get("images")
                if not images:
                    continue
                changed = False
                for image in images:
                    path = image.get("path")
                    if not (image.get("provider") == "builtin" and isinstance(path, str)):
                        continue
                    norm = path.replace("\\", "/")
                    if genre_dir_marker in norm and norm.endswith(".svg"):
                        image["path"] = f"{GENRE_ICONS_DIR_NAME}/{norm.rsplit('/', 1)[-1]}"
                        changed = True
                if changed:
                    await self.database.update(
                        DB_TABLE_GENRES,
                        {"item_id": db_row["item_id"]},
                        {"metadata": serialize_to_json(metadata)},
                    )

        if prev_version <= 41:
            # databases from before the userid column still carry the original inline
            # UNIQUE(item_id, provider, media_type) constraint, which ALTER TABLE could not
            # remove. It collides with the per-user upsert (ON CONFLICT on 4 columns) and
            # raises IntegrityError on every replay of an item. SQLite can only drop an
            # inline constraint by rebuilding the table.
            stale_unique = False
            for index in await self.database.get_rows_from_query(
                f"PRAGMA index_list({DB_TABLE_PLAYLOG})", limit=0
            ):
                if not index["unique"]:
                    continue
                index_columns = {
                    column["name"]
                    for column in await self.database.get_rows_from_query(
                        f"PRAGMA index_info({index['name']})", limit=0
                    )
                }
                if "userid" not in index_columns:
                    stale_unique = True
                    break
            if stale_unique:
                self.logger.info("Rebuilding playlog table to update its unique constraint")
                await self.database.execute(
                    f"ALTER TABLE {DB_TABLE_PLAYLOG} RENAME TO {DB_TABLE_PLAYLOG}_old"
                )
                await self.database.execute(
                    f"""CREATE TABLE {DB_TABLE_PLAYLOG}(
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
                        [queue_id] TEXT,
                        [user_initiated] BOOLEAN NOT NULL DEFAULT 1,
                        UNIQUE(item_id, provider, media_type, userid));"""
                )
                # rows from before the userid column existed have no owner and cannot be
                # kept under the NOT NULL schema
                await self.database.execute(
                    f"INSERT INTO {DB_TABLE_PLAYLOG} "
                    "(id, item_id, provider, media_type, name, image, timestamp, "
                    "fully_played, seconds_played, userid, queue_id, user_initiated) "
                    "SELECT id, item_id, provider, media_type, name, image, timestamp, "
                    "fully_played, seconds_played, userid, queue_id, user_initiated "
                    f"FROM {DB_TABLE_PLAYLOG}_old WHERE userid IS NOT NULL"
                )
                await self.database.execute(f"DROP TABLE {DB_TABLE_PLAYLOG}_old")

        if prev_version <= 42:
            audio_analysis_table_exists = await self.database.get_rows_from_query(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :table_name",
                {"table_name": DB_TABLE_AUDIO_ANALYSIS},
                limit=1,
            )
            if audio_analysis_table_exists:
                # SQLite does not guarantee WHERE-term evaluation order, so a bare
                # json_valid() term cannot reliably shield json_each()/json_type()
                # from raising on malformed rows - guard their input directly instead
                result = await self.database.execute(
                    f"""WITH RECURSIVE
                    null_centroids AS (
                        SELECT
                            aa.id,
                            '$.spectral_centroid[' || centroid.key || ']' AS path,
                            row_number() OVER (
                                PARTITION BY aa.id
                                ORDER BY CAST(centroid.key AS INTEGER)
                            ) AS sequence
                        FROM {DB_TABLE_AUDIO_ANALYSIS} AS aa,
                            json_each(
                                CASE WHEN json_valid(aa.analysis_data)
                                    THEN aa.analysis_data END,
                                '$.spectral_centroid'
                            ) AS centroid
                        WHERE aa.aa_provider_domain = :aa_provider_domain
                            AND aa.analysis_data LIKE '%null%'
                            AND json_type(
                                CASE WHEN json_valid(aa.analysis_data)
                                    THEN aa.analysis_data END,
                                '$.spectral_centroid'
                            ) = 'array'
                            AND centroid.type = 'null'
                    ),
                    repaired(id, analysis_data, sequence) AS (
                        SELECT aa.id, aa.analysis_data, 0
                        FROM {DB_TABLE_AUDIO_ANALYSIS} AS aa
                        WHERE aa.id IN (SELECT id FROM null_centroids)

                        UNION ALL

                        SELECT
                            repaired.id,
                            json_set(repaired.analysis_data, null_centroids.path, 0.0),
                            null_centroids.sequence
                        FROM repaired
                        JOIN null_centroids
                            ON null_centroids.id = repaired.id
                            AND null_centroids.sequence = repaired.sequence + 1
                    )
                    UPDATE {DB_TABLE_AUDIO_ANALYSIS} AS aa
                    SET analysis_data = (
                        SELECT repaired.analysis_data
                        FROM repaired
                        WHERE repaired.id = aa.id
                        ORDER BY repaired.sequence DESC
                        LIMIT 1
                    )
                    WHERE aa.id IN (SELECT id FROM null_centroids)""",
                    {"aa_provider_domain": "smart_fades"},
                )
                if result.rowcount:
                    self.logger.info(
                        "Repaired null spectral centroid values in %d Smart Fades "
                        "audio analysis row(s)",
                        result.rowcount,
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
                [queue_id] TEXT,
                [user_initiated] BOOLEAN NOT NULL DEFAULT 1,
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
            [search_sort_name] TEXT NOT NULL,
            [supported_mediatypes] json NOT NULL DEFAULT '[\"track\"]',
            [is_dynamic] BOOLEAN NOT NULL DEFAULT 0
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
            CREATE TABLE IF NOT EXISTS {DB_TABLE_GENRES}(
            [item_id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [name] TEXT NOT NULL,
            [sort_name] TEXT NOT NULL,
            [translation_key] TEXT,
            [description] TEXT,
            [favorite] BOOLEAN NOT NULL DEFAULT 0,
            [metadata] json NOT NULL,
            [external_ids] json NOT NULL,
            [genre_aliases] json NOT NULL DEFAULT '[]',
            [play_count] INTEGER NOT NULL DEFAULT 0,
            [last_played] INTEGER NOT NULL DEFAULT 0,
            [timestamp_added] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
            [timestamp_modified] INTEGER NOT NULL DEFAULT 0,
            [search_name] TEXT NOT NULL,
            [search_sort_name] TEXT NOT NULL,
            [is_excluded] BOOLEAN NOT NULL DEFAULT 0,
            [is_default] BOOLEAN NOT NULL DEFAULT 0
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(
            [genre_id] INTEGER NOT NULL,
            [media_id] INTEGER NOT NULL,
            [media_type] TEXT NOT NULL,
            [alias] TEXT,
            [is_derived] BOOLEAN NOT NULL DEFAULT 0,
            [is_manual] BOOLEAN NOT NULL DEFAULT 0,
            FOREIGN KEY([genre_id]) REFERENCES [genres]([item_id]),
            UNIQUE(genre_id, media_id, media_type)
            );"""
        )
        await self.database.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(
            [genre_id] INTEGER NOT NULL,
            [media_id] INTEGER NOT NULL,
            [media_type] TEXT NOT NULL,
            FOREIGN KEY([genre_id]) REFERENCES [genres]([item_id]),
            UNIQUE(genre_id, media_id, media_type)
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
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_AUDIO_ANALYSIS}(
                    [id] INTEGER PRIMARY KEY AUTOINCREMENT,
                    [media_type] TEXT NOT NULL,
                    [item_id] TEXT NOT NULL,
                    [provider] TEXT NOT NULL,
                    [aa_provider_domain] TEXT NOT NULL,
                    [analysis_data] json NOT NULL,
                    [analysis_version] INTEGER DEFAULT 1,
                    [timestamp_created] INTEGER DEFAULT (cast(strftime('%s','now') as int)),
                    UNIQUE(item_id,provider,aa_provider_domain,media_type));"""
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
            DB_TABLE_GENRES,
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
        # indexes on genre_media_item_mapping table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_media_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(media_id,media_type);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}_genre_alias_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(genre_id,alias);"
        )
        # indexes on genre_media_item_exclusion table
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}_media_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(media_id,media_type);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}_genre_idx "
            f"on {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(genre_id);"
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
            "genres",
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
            async for db_item in ctrl.iter_library_items(
                provider=list(multi_instance_providers), library_items_only=False
            ):
                if self.match_provider_instances(db_item):
                    # ctrl is the per-type controller, so it matches db_item's runtime type
                    await cast("MediaControllerBase[MediaItemType]", ctrl).update_item_in_library(
                        db_item.item_id, db_item
                    )
                # prevent overwhelming the event loop
                await asyncio.sleep(0.2)
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

    def library_supported(self, provider: Provider, media_type: MediaType) -> bool:
        """Return whether the provider declares LIBRARY support for the given media type."""
        if provider.type != ProviderType.MUSIC:
            return False
        if media_type == MediaType.ARTIST:
            return provider.supports_feature(ProviderFeature.LIBRARY_ARTISTS)
        if media_type == MediaType.ALBUM:
            return provider.supports_feature(ProviderFeature.LIBRARY_ALBUMS)
        if media_type == MediaType.TRACK:
            return provider.supports_feature(ProviderFeature.LIBRARY_TRACKS)
        if media_type == MediaType.PLAYLIST:
            return provider.supports_feature(ProviderFeature.LIBRARY_PLAYLISTS)
        if media_type == MediaType.RADIO:
            return provider.supports_feature(ProviderFeature.LIBRARY_RADIOS)
        if media_type == MediaType.AUDIOBOOK:
            return provider.supports_feature(ProviderFeature.LIBRARY_AUDIOBOOKS)
        if media_type == MediaType.PODCAST:
            return provider.supports_feature(ProviderFeature.LIBRARY_PODCASTS)
        return False

    def library_edit_supported(self, provider: Provider, media_type: MediaType) -> bool:
        """Return whether the provider supports library add/remove for the given media type."""
        if provider.type != ProviderType.MUSIC:
            return False
        if media_type == MediaType.ARTIST:
            return provider.supports_feature(ProviderFeature.LIBRARY_ARTISTS_EDIT)
        if media_type == MediaType.ALBUM:
            return provider.supports_feature(ProviderFeature.LIBRARY_ALBUMS_EDIT)
        if media_type == MediaType.TRACK:
            return provider.supports_feature(ProviderFeature.LIBRARY_TRACKS_EDIT)
        if media_type == MediaType.PLAYLIST:
            return provider.supports_feature(ProviderFeature.LIBRARY_PLAYLISTS_EDIT)
        if media_type == MediaType.RADIO:
            return provider.supports_feature(ProviderFeature.LIBRARY_RADIOS_EDIT)
        if media_type == MediaType.AUDIOBOOK:
            return provider.supports_feature(ProviderFeature.LIBRARY_AUDIOBOOKS_EDIT)
        if media_type == MediaType.PODCAST:
            return provider.supports_feature(ProviderFeature.LIBRARY_PODCASTS_EDIT)
        return False

    def library_favorites_edit_supported(self, provider: Provider, media_type: MediaType) -> bool:
        """Return whether the provider supports favorites add/remove for the given media type."""
        if provider.type != ProviderType.MUSIC:
            return False
        if media_type == MediaType.ARTIST:
            return provider.supports_feature(ProviderFeature.FAVORITE_ARTISTS_EDIT)
        if media_type == MediaType.ALBUM:
            return provider.supports_feature(ProviderFeature.FAVORITE_ALBUMS_EDIT)
        if media_type == MediaType.TRACK:
            return provider.supports_feature(ProviderFeature.FAVORITE_TRACKS_EDIT)
        if media_type == MediaType.PLAYLIST:
            return provider.supports_feature(ProviderFeature.FAVORITE_PLAYLISTS_EDIT)
        if media_type == MediaType.RADIO:
            return provider.supports_feature(ProviderFeature.FAVORITE_RADIOS_EDIT)
        if media_type == MediaType.AUDIOBOOK:
            return provider.supports_feature(ProviderFeature.FAVORITE_AUDIOBOOKS_EDIT)
        if media_type == MediaType.PODCAST:
            return provider.supports_feature(ProviderFeature.FAVORITE_PODCASTS_EDIT)
        return False

    def library_sync_back_enabled(self, provider: Provider, media_type: MediaType) -> bool:
        """Return whether library sync back is enabled for the provider+media_type."""
        conf_value = provider.config.get_value(
            CONF_ENTRY_LIBRARY_SYNC_BACK.key, CONF_ENTRY_LIBRARY_SYNC_BACK.default_value
        )
        return bool(conf_value)

    async def _credit_artist_plays(
        self,
        artists: Iterable[Artist | ItemMapping],
        *,
        timestamp: float,
        user_ids: list[str],
        queue_id: str | None,
        skip_ids: set[str],
    ) -> None:
        """Credit each (library-resolvable) artist with a play, skipping skip_ids."""
        # ON CONFLICT keeps an explicit user-initiated artist play sticky across the
        # repeated side-effect credits its tracks generate.
        upsert_query = (
            f"INSERT INTO {DB_TABLE_PLAYLOG} "
            "(item_id, provider, media_type, name, image, fully_played, "
            "seconds_played, timestamp, queue_id, user_initiated, userid) "
            "VALUES (:item_id, :provider, :media_type, :name, :image, :fully_played, "
            ":seconds_played, :timestamp, :queue_id, :user_initiated, :userid) "
            "ON CONFLICT(item_id, provider, media_type, userid) DO UPDATE SET "
            "name = excluded.name, image = excluded.image, "
            "fully_played = excluded.fully_played, seconds_played = excluded.seconds_played, "
            "timestamp = excluded.timestamp, queue_id = excluded.queue_id, "
            f"user_initiated = {DB_TABLE_PLAYLOG}.user_initiated OR excluded.user_initiated"
        )
        for artist in artists:
            db_artist = await self.artists.get_library_item_by_prov_id(
                artist.item_id, artist.provider
            )
            if db_artist is None:
                continue
            if db_artist.item_id in skip_ids:
                self.logger.debug("Skipping already-credited artist '%s'", db_artist.name)
                continue
            await self.database.execute(
                f"UPDATE {self.artists.db_table} SET play_count = play_count + 1, "
                f"last_played = {timestamp} WHERE item_id = {db_artist.item_id}"
            )
            self.logger.debug("Credited play for artist '%s'", db_artist.name)
            playlog_entry: dict[str, Any] = {
                "item_id": db_artist.item_id,
                "provider": "library",
                "media_type": MediaType.ARTIST.value,
                "name": db_artist.name,
                "image": serialize_to_json(db_artist.image.to_dict()) if db_artist.image else None,
                "fully_played": True,
                "seconds_played": None,
                "timestamp": timestamp,
                "queue_id": queue_id,
                "user_initiated": False,
            }
            for user_id in user_ids:
                playlog_entry["userid"] = user_id
                await self.database.execute(upsert_query, playlog_entry)
