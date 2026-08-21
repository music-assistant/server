"""MusicController: Orchestrates all data from music providers and sync to internal database."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from music_assistant_models.auth import Scope
from music_assistant_models.background_task import BackgroundTask, TaskMetadata, TaskSchedule
from music_assistant_models.config_entries import (
    ConfigActionResult,
    ConfigEntry,
    ConfigValueType,
)
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
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    SoundEffect,
    Track,
)
from music_assistant_models.media_items.media_item import MediaCollection

from music_assistant.constants import (
    CONF_ENTRY_LIBRARY_SYNC_BACK,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACK_ARTISTS,
    DB_TABLE_TRACKS,
    PROVIDERS_WITH_SHAREABLE_URLS,
)
from music_assistant.controllers.music.constants import (
    CACHE_CATEGORY_SEARCH_RESULTS,
    CONF_DELETED_PROVIDERS,
    CONF_RESET_DB,
    CONF_TRACK_RECONCILIATION_CURSOR,
    CONF_TRACK_RECONCILIATION_RESCAN_DUE,
    DATABASE_CLEANUP_TASK_ID,
    DB_SCHEMA_VERSION,
    INITIAL_SYNC_DELAY,
    MUSIC_SYNC_COMPLETION_CHECK_TASK_ID,
    PROVIDER_MAPPING_CORRECTION_TASK_ID,
    SEARCH_CACHE_EXPIRATION_COMBINED,
    SEARCH_CACHE_EXPIRATION_LOCAL_PROVIDER,
    SEARCH_CACHE_EXPIRATION_STREAMING_PROVIDER,
    SEARCH_PROVIDER_HARD_TIMEOUT,
    SEARCH_PROVIDER_SOFT_TIMEOUT,
    TRACK_RECONCILIATION_BATCH_SIZE,
    TRACK_RECONCILIATION_MAX_DURATION_DELTA,
    TRACK_RECONCILIATION_TASK_ID,
)
from music_assistant.controllers.music.database import (
    PLAYLOG_CONFLICT_KEYS,
    MusicDatabaseSetupMixin,
)
from music_assistant.controllers.music.helpers import filter_search_results, sort_search_result
from music_assistant.controllers.music.media.albums import AlbumsController
from music_assistant.controllers.music.media.artists import ArtistsController
from music_assistant.controllers.music.media.audiobooks import AudiobooksController
from music_assistant.controllers.music.media.base import SUPPRESS_MEDIA_ITEM_UPDATES
from music_assistant.controllers.music.media.genres import GenreController
from music_assistant.controllers.music.media.playlists import PlaylistController
from music_assistant.controllers.music.media.podcasts import PodcastsController
from music_assistant.controllers.music.media.radio import RadioController
from music_assistant.controllers.music.media.tracks import TracksController
from music_assistant.controllers.music.recency import RecencyEngine
from music_assistant.controllers.music.recommendations.controller import (
    RecommendationsController,
)
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.api import api_command
from music_assistant.helpers.collections import get_collection_item_media_type_from_item_id
from music_assistant.helpers.compare import (
    ALBUM_RETAIL_SUFFIX_KEYS,
    album_retail_suffix_sql_match,
    compare_album_name,
    compare_strings,
    compare_track,
    compare_version,
)
from music_assistant.helpers.database import UNSET, DatabaseConnection
from music_assistant.helpers.datetime import (
    from_utc_timestamp,
    local_clock_time_to_utc,
    utc_timestamp,
)
from music_assistant.helpers.json import json_loads, serialize_to_json
from music_assistant.helpers.tags import split_artists
from music_assistant.helpers.uri import parse_uri
from music_assistant.helpers.util import parse_optional_bool, parse_title_and_version
from music_assistant.models.core_controller import CoreController
from music_assistant.models.music_provider import LIBRARY_FEATURE_BY_MEDIA_TYPE, MusicProvider
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.auth import User
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.media_items import Audiobook

    from music_assistant import MusicAssistant
    from music_assistant.controllers.music.media.base import MediaControllerBase
    from music_assistant.helpers.json import SerializableType
    from music_assistant.models import ProviderInstanceType
    from music_assistant.models.provider import Provider
    from music_assistant.providers.builtin import BuiltinProvider


class RecentPlayedTrack(NamedTuple):
    """A recently played track from the playlog, with the artists recorded at play time."""

    track: ItemMapping
    artists: list[ItemMapping]


def _album_title_match(base: str, other: str) -> str:
    """
    Return a query part relating two album rows that may name the same album.

    :param base: Alias of the album row the match is expressed against.
    :param other: Alias of the album row related to it.
    """
    # a provider that spells out the retail suffix stores the album under the plain name
    # plus that suffix, so the pair is related from either side. The raw title decides which
    # side spelled it out, so an ordinary title that merely ends in those letters ("Step") is
    # left alone. This relates more titles than the album comparison accepts, which is what
    # confirms the pair afterwards.
    matches = [f"{other}.search_name = {base}.search_name"]
    for suffix in ALBUM_RETAIL_SUFFIX_KEYS:
        matches.append(
            f"({album_retail_suffix_sql_match(f'{other}.name', suffix)} "
            f"AND {other}.search_name = {base}.search_name || '{suffix}')"
        )
        matches.append(
            f"({album_retail_suffix_sql_match(f'{base}.name', suffix)} "
            f"AND {other}.search_name = "
            f"substr({base}.search_name, 1, length({base}.search_name) - {len(suffix)}))"
        )
    return " OR ".join(matches)


# Selects pairs of library track rows that are likely the same recording held twice,
# once per music provider. Both rows must carry the same normalized title, share a track
# artist and sit within a few seconds of each other. The album term is the decisive one:
# both rows must appear at the same position on an album with the same title, so the merge
# always rests on two providers agreeing on where the track belongs rather than on title and
# duration alone. Titles are related loosely enough to see past a spelled-out retail suffix,
# leaving the identity for the album comparison the pair is then held to. Titles that
# normalize to nothing (symbol-only album names) are excluded there, as they would match
# every other such album. Rows that already share a provider are skipped, as a provider
# listing the same recording twice is a separate (and far riskier) case.
_DUPLICATE_TRACK_CANDIDATES_QUERY = f"""
SELECT t1.item_id AS item_id_1, t2.item_id AS item_id_2
FROM {DB_TABLE_TRACKS} t1
JOIN {DB_TABLE_TRACKS} t2
  ON t2.search_name = t1.search_name
 AND t2.item_id > t1.item_id
 AND abs(t2.duration - t1.duration) <= :max_duration_delta
WHERE (t1.item_id > :cursor_item_id_1
       OR (t1.item_id = :cursor_item_id_1 AND t2.item_id > :cursor_item_id_2))
  AND EXISTS (
    SELECT 1 FROM {DB_TABLE_TRACK_ARTISTS} ta1
    JOIN {DB_TABLE_TRACK_ARTISTS} ta2
      ON ta2.artist_id = ta1.artist_id AND ta2.track_id = t2.item_id
    WHERE ta1.track_id = t1.item_id)
  AND EXISTS (
    SELECT 1 FROM {DB_TABLE_ALBUM_TRACKS} at1
    JOIN {DB_TABLE_ALBUMS} al1 ON al1.item_id = at1.album_id
    JOIN {DB_TABLE_ALBUM_TRACKS} at2 ON at2.track_id = t2.item_id
    JOIN {DB_TABLE_ALBUMS} al2
      ON al2.item_id = at2.album_id AND ({_album_title_match("al1", "al2")})
    WHERE at1.track_id = t1.item_id
      -- a title that is nothing but the suffix strips to nothing, which would relate it to
      -- every symbol-only album, so neither side may normalize away
      AND al1.search_name != ''
      AND al2.search_name != ''
      -- an unreported position is stored as 0, so two of those agree on nothing;
      -- a missing disc number does read as disc 1, the way compare_track takes it
      -- for local files that carry no disc tag
      AND at1.track_number > 0
      AND coalesce(nullif(at1.disc_number, 0), 1) = coalesce(nullif(at2.disc_number, 0), 1)
      AND at1.track_number = at2.track_number)
  AND NOT EXISTS (
    SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} pm1
    JOIN {DB_TABLE_PROVIDER_MAPPINGS} pm2
      ON pm2.provider_domain = pm1.provider_domain
     AND pm2.media_type = 'track' AND pm2.item_id = t2.item_id
    WHERE pm1.media_type = 'track' AND pm1.item_id = t1.item_id)
ORDER BY t1.item_id, t2.item_id
"""

# Returns the title and edition of every album appearance that made the two tracks a
# candidate, so the pair can be held to agreeing on both. The album terms mirror the candidate
# query exactly: an appearance the pair does not share a position on says nothing about the
# album of the one it does.
_SHARED_ALBUM_EDITIONS_QUERY = f"""
SELECT al1.name AS name_1, al2.name AS name_2,
       al1.version AS version_1, al2.version AS version_2
FROM {DB_TABLE_ALBUM_TRACKS} at1
JOIN {DB_TABLE_ALBUMS} al1 ON al1.item_id = at1.album_id
JOIN {DB_TABLE_ALBUM_TRACKS} at2 ON at2.track_id = :item_id_2
JOIN {DB_TABLE_ALBUMS} al2
  ON al2.item_id = at2.album_id AND ({_album_title_match("al1", "al2")})
WHERE at1.track_id = :item_id_1
  AND al1.search_name != ''
  AND al2.search_name != ''
  AND at1.track_number > 0
  AND coalesce(nullif(at1.disc_number, 0), 1) = coalesce(nullif(at2.disc_number, 0), 1)
  AND at1.track_number = at2.track_number
"""


class MusicController(MusicDatabaseSetupMixin, CoreController):
    """Several helpers around the musicproviders."""

    domain: str = "music"
    config: CoreConfig
    # where the duplicate track walk stands; restored from config on startup
    _track_reconciliation_cursor: tuple[int, int] | None = (0, 0)
    _track_reconciliation_rescan_due: bool = False

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
        self.recommendations = RecommendationsController(self.mass)
        self.recency = RecencyEngine(self.mass)
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

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        return (
            ConfigEntry(
                key=CONF_RESET_DB,
                type=ConfigEntryType.ACTION,
                category="generic",
                advanced=True,
            ),
        )

    async def handle_config_action(
        self, action: str
    ) -> tuple[ConfigEntry, ...] | ConfigActionResult | None:
        """Handle a one-shot action button press and report its outcome."""
        if action == CONF_RESET_DB:
            await self._reset_database()
            await self.mass.cache.clear()
            await self.start_sync()
            return ConfigActionResult(translation_key=f"{CONF_RESET_DB}.result")
        return await super().handle_config_action(action)

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
        self._restore_track_reconciliation_state()
        self._register_track_reconciliation_task()
        self.genres.register_scheduled_scan_task()

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._database:
            await self._database.close()

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        return {
            "db_schema_version": DB_SCHEMA_VERSION,
            "sync_tasks_active": len(self.active_sync_tasks),
        }

    async def on_provider_loaded(self, provider: MusicProvider) -> None:
        """Handle logic when a provider is loaded."""
        await self.schedule_provider_sync(provider.instance_id)

    async def on_provider_unload(self, provider: MusicProvider) -> None:
        """
        Handle logic when a provider is (about to get) unloaded.

        Sync tasks are unscheduled by MusicAssistant.unload_provider itself, which also
        decides whether their persisted state is kept (reload) or cleared (removal).
        """

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

    @api_command("music/sync", required_scope=Scope.LIBRARY_MANAGE)
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
                            translation_owner=self.translation_owner,
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

    @api_command("music/search", required_scope=Scope.LIBRARY_READ, allow_impersonation=True)
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] = MediaType.ALL,
        limit: int = 25,
        library_only: bool = False,
        providers: list[str] | None = None,
    ) -> SearchResults:
        """
        Perform global search for media items on all providers.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: number of items to return in the search (per type).
        :param library_only: Deprecated - use providers=["library"] instead.
        :param providers: Optionally restrict the search to the given providers
            (by instance id or domain), where the special value "library" selects
            the library. Omit to search the library and all available providers.
        """
        if not search_query.strip():
            # several providers reject an empty query with a hard error
            return SearchResults()
        if not media_types:
            media_types = MediaType.ALL
        if library_only and providers is None:
            # handle deprecated library_only flag
            providers = ["library"]
        # resolve the search targets: all (unique) music providers plus plugin
        # providers with search support, optionally filtered by the providers argument
        plugin_search_providers = [
            p.instance_id
            for p in self.mass.get_providers_supporting_feature(
                ProviderFeature.SEARCH,
                priority=(ProviderType.PLUGIN,),
            )
        ]
        all_search_providers = sorted(self.get_unique_providers() + plugin_search_providers)
        if providers is None:
            include_library = True
            search_providers = all_search_providers
        else:
            include_library = "library" in providers
            requested_providers = set(providers)
            search_providers = [
                instance_id
                for instance_id in all_search_providers
                if (prov := self.mass.get_provider(instance_id))
                and (prov.instance_id in requested_providers or prov.domain in requested_providers)
            ]
        # use cache to avoid repeated searches
        cache_key = (
            f"{search_query}-{'-'.join(sorted([mt.value for mt in media_types]))}-{limit}-"
            f"{int(include_library)}-{','.join(search_providers)}"
        )
        if cache := await self.mass.cache.get(
            key=cache_key,
            provider=self.domain,
            category=CACHE_CATEGORY_SEARCH_RESULTS,
            base_class=SearchResults,
        ):
            return cast("SearchResults", cache)
        # Check if the search query is a streaming provider public shareable URL
        if (url_result := await self._search_shareable_url(search_query)) is not None:
            return url_result
        # handle normal global search by querying the library and all providers
        # the library is always searched first: it is fast and its results are used
        # to deduplicate provider results and to skip provider searches for media
        # types that already have a (near) exact match in the library
        library_results = await self.search_library(search_query, media_types, limit=limit)
        results_per_provider: list[SearchResults] = []
        if include_library:
            results_per_provider.append(library_results)
        all_results_complete = True
        if search_providers:
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
            # only apply the exact match shortcut on a regular global search;
            # an explicit providers selection must always search those providers
            covered_media_types = (
                self._get_covered_media_types(library_results, search_query)
                if providers is None
                else set()
            )
            provider_searches: list[Coroutine[Any, Any, SearchResults | None]] = []
            for provider_instance in search_providers:
                if not (prov := self.mass.get_provider(provider_instance)):
                    continue
                # skip media types for which the library already holds a (near)
                # exact match that is mapped to this provider: searching the
                # provider again for that media type will not add anything new
                prov_media_types = [
                    mt
                    for mt in media_types
                    if (mt, prov.domain) not in covered_media_types
                    and (mt, prov.instance_id) not in covered_media_types
                ]
                if not prov_media_types:
                    continue
                provider_searches.append(
                    self._search_provider(
                        search_query,
                        provider_instance,
                        prov_media_types,
                        limit=limit,
                        skip_item_ids=all_prov_item_ids,
                    )
                )
            # include results from all (unique) music providers
            # one failing provider must not break the entire search,
            # so exceptions are logged and excluded from the results
            gather_results = await asyncio.gather(*provider_searches, return_exceptions=True)
            for res in gather_results:
                if isinstance(res, SearchResults):
                    results_per_provider.append(res)
                    continue
                # a provider that failed or timed out contributes no results
                all_results_complete = False
                if isinstance(res, BaseException):
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
            genres=[
                item
                for sublist in zip_longest(*[x.genres for x in results_per_provider])
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
            sound_effects=[
                item
                for sublist in zip_longest(*[x.sound_effects for x in results_per_provider])
                for item in sublist
                if item is not None
            ][:limit],
        )

        # the search results should already be sorted by relevance
        # but we apply one extra round of sorting and that is to put exact name
        # matches and library items first
        for field in (
            "artists",
            "albums",
            "genres",
            "tracks",
            "playlists",
            "radio",
            "audiobooks",
            "podcasts",
            "sound_effects",
        ):
            setattr(result, field, sort_search_result(search_query, getattr(result, field)))
        # only cache the combined result if all providers contributed,
        # so a failed or timed out provider is retried on a next search
        if all_results_complete:
            await self._cache_search_results(
                cache_key, result, SEARCH_CACHE_EXPIRATION_COMBINED, self.domain
            )
        return result

    async def search_library(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 10,
    ) -> SearchResults:
        """
        Perform search on the library.

        :param search_query: Search query
        :param media_types: A list of media_types to include.
        :param limit: number of items to return in the search (per type).
        """
        result_fields: dict[MediaType, str] = {
            MediaType.ARTIST: "artists",
            MediaType.ALBUM: "albums",
            MediaType.GENRE: "genres",
            MediaType.TRACK: "tracks",
            MediaType.PLAYLIST: "playlists",
            MediaType.RADIO: "radio",
            MediaType.AUDIOBOOK: "audiobooks",
            MediaType.PODCAST: "podcasts",
        }
        result = SearchResults()
        # search all media types in parallel, each is an independent db query
        searchable_media_types = [x for x in media_types if x in result_fields]
        search_results = await asyncio.gather(
            *[
                self.get_controller(media_type).search(search_query, "library", limit=limit)
                for media_type in searchable_media_types
            ]
        )
        for media_type, items in zip(searchable_media_types, search_results, strict=True):
            if items:
                setattr(result, result_fields[media_type], items)
        return result

    @api_command("music/browse", required_scope=Scope.LIBRARY_READ)
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

    @api_command("music/recently_played_items", required_scope=Scope.LIBRARY_READ)
    async def recently_played(
        self,
        limit: int = 10,
        media_types: list[MediaType] | None = None,
        userid: str | None = None,
        queue_id: str | None = None,
        fully_played_only: bool = True,
        user_initiated_only: bool = False,
        played_after_timestamp: int | None = None,
        providers: list[str] | None = None,
        *,
        always_include_media_types: list[MediaType] | None = None,
    ) -> list[ItemMapping]:
        """
        Return a list of the last played items.

        :param limit: Maximum number of items to return.
        :param media_types: Filter by media types.
        :param userid: Filter by specific user ID.
        :param queue_id: Filter by specific queue ID.
        :param fully_played_only: If True, only return fully played items.
        :param user_initiated_only: If True, only return items initiated by the user.
        :param played_after_timestamp: If set, only return items played at or after this
            epoch-seconds timestamp.
        :param providers: Restrict results to items reachable through one of these provider
            instance ids (OR semantics). None applies no filter; an explicit empty list
            returns no items.
        :param always_include_media_types: Media types to include regardless of
            user_initiated_only (e.g. podcasts/audiobooks, which have no user-initiated
            container).
        """
        if providers is not None and not providers:
            return []
        if media_types is None:
            media_types = MediaType.ALL
        media_types_str = "(" + ",".join(f'"{x}"' for x in media_types) + ")"
        available_providers = ("library", *self.get_active_provider_instances())
        available_providers_str = "(" + ",".join(f'"{x}"' for x in available_providers) + ")"
        # user_initiated_only constrains only `media_types`; always_include_media_types are
        # included regardless (e.g. podcasts/audiobooks have no user-initiated container row).
        media_type_clause = f"p.media_type in {media_types_str}"
        if user_initiated_only:
            media_type_clause += " AND p.user_initiated = 1"
        media_type_clause = f"({media_type_clause})"
        if always_include_media_types:
            always_str = "(" + ",".join(f'"{x}"' for x in always_include_media_types) + ")"
            media_type_clause = f"({media_type_clause} OR p.media_type in {always_str})"

        params: dict[str, Any] = {}
        user = get_current_user()
        # a library row only needs resolving through its provider mappings when a filter
        # (explicit or user-scoped) is actually active; otherwise every library row is
        # kept, matching this method's unfiltered behavior.
        if providers is not None or (user and user.provider_filter):
            requested_clause = ""
            direct_requested_clause = ""
            if providers is not None:
                params["requested_providers"] = providers
                requested_clause = " AND m.provider_instance IN :requested_providers"
                direct_requested_clause = " AND p.provider IN :requested_providers"
            provider_clause = (
                "(CASE WHEN p.provider = 'library' THEN "
                f"EXISTS (SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} m "
                "WHERE m.item_id = p.item_id AND m.media_type = p.media_type "
                f"AND m.available = 1 "
                f"AND m.provider_instance IN {available_providers_str}{requested_clause}) "
                f"ELSE (p.provider IN {available_providers_str}{direct_requested_clause}) END)"
            )
        else:
            provider_clause = f"p.provider IN {available_providers_str}"
        query = (
            f"SELECT p.* FROM {DB_TABLE_PLAYLOG} p WHERE {media_type_clause} AND {provider_clause} "
        )
        if fully_played_only:
            query += "AND p.fully_played = 1 "
        if userid:
            query += "AND p.userid = :userid "
            params["userid"] = userid
        elif user:
            query += "AND p.userid = :userid "
            params["userid"] = user.user_id
        if queue_id:
            query += "AND p.queue_id = :queue_id "
            params["queue_id"] = queue_id
        if played_after_timestamp is not None:
            query += "AND p.timestamp >= :played_after_timestamp "
            params["played_after_timestamp"] = played_after_timestamp
        query += "ORDER BY p.timestamp DESC"
        db_rows = await self.mass.music.database.get_rows_from_query(
            query, params=params or None, limit=limit
        )
        result: list[ItemMapping] = []
        available_providers = ("library", *get_global_cache_value("available_providers", []))
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

    async def recently_played_tracks(
        self,
        limit: int,
        played_after_timestamp: int,
        userid: str | None = None,
    ) -> list[RecentPlayedTrack]:
        """
        Return recently played, fully played tracks with their recorded artists, newest first.

        :param limit: Maximum number of plays to return.
        :param played_after_timestamp: Only include plays at or after this epoch-seconds timestamp.
        :param userid: Restrict to this user (defaults to the current session user, else all users).
        """
        query = (
            f"SELECT item_id, provider, name, image, artists FROM {DB_TABLE_PLAYLOG} "
            "WHERE media_type = 'track' AND fully_played = 1 "
            "AND timestamp >= :played_after_timestamp "
        )
        params: dict[str, Any] = {"played_after_timestamp": played_after_timestamp}
        if userid:
            query += "AND userid = :userid "
            params["userid"] = userid
        elif user := get_current_user():
            query += "AND userid = :userid "
            params["userid"] = user.user_id
        query += "ORDER BY timestamp DESC"
        db_rows = await self.mass.music.database.get_rows_from_query(
            query, params=params, limit=limit
        )
        available_providers = ("library", *get_global_cache_value("available_providers", []))
        return [
            RecentPlayedTrack(
                track=ItemMapping.from_dict(
                    {
                        "item_id": db_row["item_id"],
                        "provider": db_row["provider"],
                        "media_type": "track",
                        "name": db_row["name"],
                        "image": json_loads(db_row["image"]) if db_row["image"] else None,
                        "available": db_row["provider"] in available_providers,
                    }
                ),
                artists=[ItemMapping.from_dict(artist) for artist in json_loads(db_row["artists"])]
                if db_row["artists"]
                else [],
            )
            for db_row in db_rows
        ]

    @api_command("music/recently_added_tracks", required_scope=Scope.LIBRARY_READ)
    async def recently_added_tracks(self, limit: int = 10) -> list[Track]:
        """Return a list of the last added tracks."""
        return await self.tracks.library_items(
            limit=limit, order_by="timestamp_added_desc", summary=False
        )

    @api_command("music/in_progress_items", required_scope=Scope.LIBRARY_READ)
    async def in_progress_items(
        self, limit: int = 10, all_users: bool = False, providers: list[str] | None = None
    ) -> list[ItemMapping]:
        """
        Return a list of the Audiobooks and PodcastEpisodes that are in progress.

        :param limit: Maximum number of items to return.
        :param all_users: If True, include in-progress items across all users, not just
            the current session's user.
        :param providers: Restrict results to items reachable through one of these provider
            instance ids (OR semantics). None applies no filter; an explicit empty list
            returns no items.
        """
        if providers is not None and not providers:
            return []
        available_providers = ("library", *self.get_active_provider_instances())
        available_providers_str = "(" + ",".join(f'"{x}"' for x in available_providers) + ")"
        params: dict[str, Any] = {}
        requested_clause = ""
        direct_requested_clause = ""
        if providers is not None:
            params["requested_providers"] = providers
            requested_clause = " AND m.provider_instance IN :requested_providers"
            direct_requested_clause = " AND p.provider IN :requested_providers"

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
            "AND m.available = 1 "
        )
        if not all_users and (user := get_current_user()):
            filter_for_str = available_providers_str
            if user.provider_filter:
                filter_for_str = "(" + ",".join(f'"{x}"' for x in user.provider_filter) + ")"
            query += (
                f"AND m.provider_instance IN {filter_for_str} "
                f"AND m.provider_instance IN {available_providers_str}"
                f"{requested_clause} "
                ") "
                f"ELSE (p.provider IN {filter_for_str} AND p.provider IN {available_providers_str}"
                f"{direct_requested_clause})"
                "END "
                ") "
                f"AND p.userid = '{user.user_id}' "
            )
        else:
            # for a library item, we still have to verify via the provider mapping table
            # that the provider is available
            query += (
                f"AND m.provider_instance IN {available_providers_str}"
                f"{requested_clause} "
                ") "
                f"ELSE p.provider IN {available_providers_str}"
                f"{direct_requested_clause} "
                "END "
                ") "
            )
        query += "ORDER BY timestamp DESC"

        db_rows = await self.mass.music.database.get_rows_from_query(
            query, params=params or None, limit=limit
        )
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

    @api_command("music/item_by_uri", required_scope=Scope.LIBRARY_READ)
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

    @api_command("music/sound_effects", required_scope=Scope.LIBRARY_READ)
    async def sound_effects(self) -> list[SoundEffect]:
        """Return all sound effect items from providers supporting them."""
        providers = self._apply_user_provider_filter(
            self.mass.get_providers_supporting_feature(ProviderFeature.SOUND_EFFECTS)
        )
        results_per_provider: list[list[SoundEffect]] = await asyncio.gather(
            *[
                self._get_provider_sound_effects(cast("MusicProvider", provider))
                for provider in providers
            ]
        )
        return [item for sublist in results_per_provider for item in sublist]

    @api_command("music/item", required_scope=Scope.LIBRARY_READ)
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
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if media_type in (
            MediaType.TRACK,
            MediaType.RADIO,
            MediaType.SOUND_EFFECT,
            MediaType.UNKNOWN,  # e.g. plain (HA) URLs, see helpers/uri.py
        ) and (
            provider_instance_id_or_domain == "builtin"
            or (provider and provider.domain == "builtin")
        ):
            # handle special case of 'builtin' MusicProvider which allows us to play regular url's
            builtin_prov = cast("BuiltinProvider", provider or self.mass.get_provider("builtin"))
            if media_type == MediaType.RADIO:
                # a radio station must stay a radio station, also when the stream
                # reports a duration or carries no ICY name
                return await builtin_prov.get_radio(item_id)
            if media_type == MediaType.TRACK:
                # and a track must stay a track, also when the stream carries an
                # ICY name or reports no duration
                return await builtin_prov.get_track(item_id)
            return await builtin_prov.parse_item(item_id, requested_media_type=media_type)
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
        if media_type == MediaType.SOUND_EFFECT:
            # Sound effects are not library-backed; resolve them live from the
            # owning music provider. Returning the live MediaItem lets play_media
            # create a queue item the standard way.
            prov = self.mass.get_provider(provider_instance_id_or_domain)
            if isinstance(prov, MusicProvider) and (
                ProviderFeature.SOUND_EFFECTS in prov.supported_features
            ):
                return await prov.get_sound_effect(item_id)
            raise MediaNotFoundError(
                f"SoundEffect {provider_instance_id_or_domain}/{item_id} not found"
            )
        if media_type == MediaType.COLLECTION:
            ctrl = self.get_controller_for_collection(item_id)
            return await ctrl.get_collection(item_id)
        ctrl = self.get_controller(media_type)
        return await ctrl.get(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            allow_update_metadata=allow_update_metadata,
        )

    @api_command("music/get_library_item", required_scope=Scope.LIBRARY_READ)
    async def get_library_item_by_prov_id(
        self,
        media_type: MediaType,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> MediaItemType | None:
        """Get the library item for the given provider item, if present."""
        ctrl = self.get_controller(media_type)
        return await ctrl.get_library_item_by_prov_id(
            item_id=item_id,
            provider_instance_id_or_domain=provider_instance_id_or_domain,
        )

    @api_command("music/favorites/add_item", required_scope=Scope.LIBRARY_WRITE)
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
            if uri_media_type in (MediaType.AUDIO_SOURCE, MediaType.SOUND_EFFECT):
                raise UnsupportedFeaturedException(
                    f"{uri_media_type.value} items can not be favorites"
                )
            # a favorite URI always resolves to a media item, never a BrowseFolder
            item = cast("MediaItemType", await self.get_item_by_uri(item))
        if item.media_type in (MediaType.AUDIO_SOURCE, MediaType.SOUND_EFFECT):
            # AudioSources and SoundEffects are live provider content (existence
            # depends on a loaded provider) and have no stable library identity,
            # so they can not be persisted as favorites.
            raise UnsupportedFeaturedException(
                f"{item.media_type.value} items can not be favorites"
            )
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

    @api_command("music/favorites/remove_item", required_scope=Scope.LIBRARY_WRITE)
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

    @api_command("music/library/remove_item", required_scope=Scope.LIBRARY_WRITE)
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

    @api_command("music/library/add_item", required_scope=Scope.LIBRARY_WRITE)
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
            if uri_media_type in (MediaType.AUDIO_SOURCE, MediaType.SOUND_EFFECT):
                raise UnsupportedFeaturedException(
                    f"{uri_media_type.value} items can not be library items"
                )
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
        if full_item.media_type in (MediaType.AUDIO_SOURCE, MediaType.SOUND_EFFECT):
            # AudioSources and SoundEffects are live provider content (existence
            # depends on a loaded provider) and have no stable library identity,
            # so they can not be persisted as library items.
            raise UnsupportedFeaturedException(
                f"{full_item.media_type.value} items can not be library items"
            )
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

    @api_command("music/refresh_item", required_scope=Scope.LIBRARY_MANAGE)
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

    @api_command("music/mark_played", required_scope=Scope.LIBRARY_WRITE)
    async def mark_item_played(
        self,
        media_item: MediaItemType | ItemMapping,
        fully_played: bool = True,
        seconds_played: int | None = None,
        is_playing: bool = False,
        userid: str | None = None,
        queue_id: str | None = None,
        user_initiated: bool = True,
        skip_artist_ids: list[str] | None = None,
        playback_speed: float | None = None,
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
            Sticky once set: a later report can promote a playlog row to user-initiated but
            never demote it, so a writer reporting playback it did not itself initiate
            (e.g. a provider sync) must pass False.
        :param skip_artist_ids: Library artist ids to skip when crediting an album's artists.
        :param playback_speed: The current playback speed to persist (audiobooks/podcasts).
            If None, any previously stored speed for the item is preserved.
        """
        timestamp = utc_timestamp()
        # we deliberately skip one-off items: sound effects and live inputs whoever owns
        # them, and everything the builtin provider plays (except playlists) is a one-off url
        if media_item.media_type in (MediaType.SOUND_EFFECT, MediaType.AUDIO_SOURCE):
            return
        if (
            media_item.provider.startswith("builtin")
            and media_item.media_type != MediaType.PLAYLIST
        ):
            return
        # the playlog is keyed by the identity the caller referenced, not the resolved one
        reference = media_item
        media_item = await self._resolve_playlog_item(media_item)

        params = {
            "item_id": reference.item_id,
            "provider": reference.provider,
            "media_type": media_item.media_type.value,
            "name": media_item.name,
            "image": serialize_to_json(media_item.image.to_dict()) if media_item.image else None,
            # store lightweight artist mappings so playlog rows can later be matched or
            # resolved by artist without an extra provider lookup
            "artists": serialize_to_json(
                [ItemMapping.from_item(artist).to_dict() for artist in artists]
            )
            if (artists := getattr(media_item, "artists", None))
            else None,
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
            # Leaving the speed out keeps whatever is already stored for this item/user
            # (a provider sync reporting progress has no speed to offer), and falls back to
            # the column default of 1.0 for a brand new row.
            if playback_speed is not None:
                params["playback_speed"] = playback_speed
            for user_id in user_ids:
                params["userid"] = user_id
                await self._upsert_playlog(params)

        # Set seconds_played in accordance with fully_played, if the media_item has
        # a duration, before it is forwarded to music_providers
        if seconds_played is None:
            seconds_played = 0
            if (
                fully_played
                and not isinstance(
                    media_item, Album | Artist | Genre | Playlist | Podcast | MediaCollection
                )
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
        if isinstance(media_item, PodcastEpisode) and media_item.podcast:
            await self._credit_podcast_play(
                media_item.podcast,
                timestamp=timestamp,
                user_ids=user_ids,
                queue_id=queue_id,
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

    @api_command("music/mark_unplayed", required_scope=Scope.LIBRARY_WRITE)
    async def mark_item_unplayed(
        self,
        media_item: MediaItemType | ItemMapping,
        userid: str | None = None,
    ) -> None:
        """
        Mark item as unplayed in playlog.

        :param media_item: The media item to mark as unplayed.
        :param all_users: If True, mark the item as unplayed for all users.
        :param userid: The user ID to mark the item as unplayed for (instead of the current user).
        """
        # the playlog is keyed by the identity the caller referenced, not the resolved one
        reference = media_item
        media_item = await self._resolve_playlog_item(media_item)
        params = {
            "item_id": reference.item_id,
            "provider": reference.provider,
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

    @api_command("music/track_by_name", required_scope=Scope.LIBRARY_READ)
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

    async def get_playback_speed(
        self, media_item: Audiobook | PodcastEpisode, userid: str | None = None
    ) -> float:
        """
        Get the stored playback speed for the given audiobook or podcast episode.

        Returns 1.0 (normal speed) when no custom speed was stored for the item,
        or when no user can be determined to scope the lookup.

        :param media_item: The audiobook or podcast episode to look up.
        :param userid: The user ID to look up the speed for (instead of the current user).
        """
        if not userid:
            if session_user := get_current_user():
                userid = session_user.user_id
            elif provider_user := await self._get_user_for_provider(media_item.provider_mappings):
                userid = provider_user.user_id
            else:
                # the speed is stored per user; without one we can't scope the lookup
                return 1.0
        db_entry = await self.database.get_row(
            DB_TABLE_PLAYLOG,
            {
                "item_id": media_item.item_id,
                "provider": media_item.provider,
                "media_type": media_item.media_type.value,
                "userid": userid,
            },
        )
        if db_entry and (stored_speed := db_entry["playback_speed"]) is not None:
            return float(stored_speed)
        return 1.0

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
        raise NotImplementedError(
            f"No media controller available for media type: {media_type.value}"
        )

    def get_controller_for_collection(
        self, item_id: str
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
        media_type = get_collection_item_media_type_from_item_id(item_id)
        controller = self.get_controller(media_type)
        if not isinstance(controller, AudiobooksController):
            # currently only supported for audiobooks
            raise NotImplementedError(
                f"No media controller available for media type: {media_type.value}"
            )
        return controller

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

    def get_active_provider_instances(self) -> list[str]:
        """
        Return the instance ids of all currently loaded, available MusicProviders.

        Unlike `get_unique_providers`, this keeps every instance of a streaming
        provider's domain instead of collapsing to one per domain, so a caller
        validating a specific requested provider instance id isn't shadowed by
        another instance of the same domain. Applies the current user's provider
        filter (via the `providers` property) and excludes providers that are
        loaded but not currently available.
        """
        return [provider.instance_id for provider in self.providers if provider.available]

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
        await self.unschedule_provider_sync(provider.instance_id, clear_persisted_state=False)
        for media_type in MediaType:
            if not self.library_supported(provider, media_type):
                continue
            await self._schedule_provider_mediatype_sync(provider, media_type, True)

    async def unschedule_provider_sync(
        self, provider_instance_id: str, clear_persisted_state: bool = True
    ) -> None:
        """
        Unschedule Library sync for given provider and wait for a running sync to stop.

        Callers tear down provider state right after this (unloading the provider, or
        rescheduling its syncs), so all media types are cancelled first and then awaited
        together, keeping the bounded wait to one timeout instead of one per media type.

        :param provider_instance_id: The provider instance id to unschedule.
        :param clear_persisted_state: Whether to remove persisted schedule state from config.
        """
        await asyncio.gather(
            *(
                self.mass.tasks.unregister_scheduled_task_and_wait(
                    self._get_sync_task_id(provider_instance_id, media_type),
                    clear_persisted_state=clear_persisted_state,
                )
                for media_type in MediaType
            )
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

    @api_command("music/add_provider_mapping", required_scope=Scope.LIBRARY_MANAGE)
    async def add_provider_mapping(
        self, media_type: MediaType, db_id: str, mapping: ProviderMapping
    ) -> None:
        """Add provider mapping to the given library item."""
        ctrl = self.get_controller(media_type)
        await ctrl.add_provider_mappings(db_id, [mapping])

    @api_command("music/remove_provider_mapping", required_scope=Scope.LIBRARY_MANAGE)
    async def remove_provider_mapping(
        self, media_type: MediaType, db_id: str, mapping: ProviderMapping
    ) -> None:
        """Remove provider mapping from the given library item."""
        ctrl = self.get_controller(media_type)
        await ctrl.remove_provider_mapping(db_id, mapping.provider_instance, mapping.item_id)

    @api_command("music/match_providers", required_scope=Scope.LIBRARY_MANAGE)
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

    def queue_provider_mapping_correction_task(self) -> BackgroundTask:
        """Queue the provider mapping correction as a managed background task."""
        self._register_provider_mapping_correction_task()
        return self.mass.tasks.run_task(PROVIDER_MAPPING_CORRECTION_TASK_ID)

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

    def library_supported(self, provider: Provider, media_type: MediaType) -> bool:
        """Return whether the provider declares LIBRARY support for the given media type."""
        if provider.type != ProviderType.MUSIC:
            return False
        if (feature := LIBRARY_FEATURE_BY_MEDIA_TYPE.get(media_type)) is None:
            return False
        return provider.supports_feature(feature)

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

    @api_command("music/item_by_name", required_scope=Scope.LIBRARY_READ, allow_impersonation=True)
    async def get_item_by_name(
        self,
        name: str,
        artist: str | None = None,
        album: str | None = None,
        media_type: MediaType | None = None,
    ) -> MediaItemType | ItemMapping | None:
        """Try to find a media item (such as a playlist) by name."""
        return await self._get_item_by_name(name, artist, album, media_type)

    @api_command(
        "music/verify_item_uri", required_scope=Scope.LIBRARY_READ, allow_impersonation=True
    )
    async def verify_item_uri(self, uri: str) -> bool:
        """
        Verify whether a uri points to a valid, accessible item.

        :param uri: The uri to verify.
        """
        return await self._handle_verify_item_uri(uri)

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

    async def _search_shareable_url(self, search_query: str) -> SearchResults | None:
        """
        Handle a search query that is a streaming provider public shareable URL.

        Returns None if the query is not such a URL and a regular search must be done.
        """
        try:
            media_type, provider_instance_id_or_domain, item_id = await parse_uri(
                search_query, validate_id=True
            )
        except InvalidProviderURI:
            return None
        except InvalidProviderID as err:
            self.logger.warning("%s", str(err))
            return SearchResults()
        if provider_instance_id_or_domain not in PROVIDERS_WITH_SHAREABLE_URLS:
            return None
        try:
            item = await self.get_item(
                media_type=media_type,
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
            )
        except MusicAssistantError as err:
            self.logger.warning("%s", str(err))
            return SearchResults()
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

    async def _search_provider(
        self,
        search_query: str,
        provider_instance_id_or_domain: str,
        media_types: list[MediaType],
        limit: int = 10,
        skip_item_ids: set[tuple[MediaType, str, str]] | None = None,
    ) -> SearchResults | None:
        """
        Perform search on given provider, returns None if the search failed or timed out.

        :param search_query: Search query
        :param provider_instance_id_or_domain: instance_id or domain of the provider
                                               to perform the search on.
        :param media_types: A list of media_types to include.
        :param limit: number of items to return in the search (per type).
        :param skip_item_ids: Optional set of (media_type, provider_domain, item_id)
                              tuples to filter out of the results.
        """
        prov = self.mass.get_provider(provider_instance_id_or_domain, provider_type=MusicProvider)
        if not prov:
            return SearchResults()
        if ProviderFeature.SEARCH not in prov.supported_features:
            return SearchResults()

        # create safe search string
        search_query = search_query.replace("/", " ").replace("'", "")
        # use the per-provider cache so repeated and overlapping searches
        # do not hit the provider again
        cache_key = f"{search_query}-{'-'.join(sorted([mt.value for mt in media_types]))}-{limit}"
        if (
            cache := await self.mass.cache.get(
                key=cache_key,
                provider=prov.instance_id,
                category=CACHE_CATEGORY_SEARCH_RESULTS,
                base_class=SearchResults,
            )
        ) is not None:
            return filter_search_results(cast("SearchResults", cache), prov.domain, skip_item_ids)
        # run the provider search as a separate task (deduplicated by task_id so
        # identical concurrent searches share a single provider call) and wait for
        # it a limited amount of time only: a slow provider then contributes no
        # results now, while its search continues in the background so the result
        # is cached and available for a next search request
        task = self.mass.create_task(
            self._execute_provider_search(prov, search_query, media_types, limit, cache_key),
            task_id=f"provider_search_{prov.instance_id}_{cache_key}",
        )
        try:
            async with asyncio.timeout(SEARCH_PROVIDER_SOFT_TIMEOUT):
                prov_search_results = await asyncio.shield(task)
        except TimeoutError:
            self.logger.warning(
                "Search on provider %s did not return in time, "
                "the search continues in the background",
                prov.name,
            )
            return None
        if prov_search_results is None:
            return None
        return filter_search_results(prov_search_results, prov.domain, skip_item_ids)

    async def _execute_provider_search(
        self,
        prov: MusicProvider,
        search_query: str,
        media_types: list[MediaType],
        limit: int,
        cache_key: str,
    ) -> SearchResults | None:
        """
        Execute the actual search on a provider and cache the result.

        Returns None if the provider search failed or timed out. All errors are
        handled here (and not raised) as this coroutine runs as a background task
        that may outlive the request that started it.
        """
        try:
            async with asyncio.timeout(SEARCH_PROVIDER_HARD_TIMEOUT):
                result = await prov.search(search_query, media_types, limit)
        except TimeoutError:
            self.logger.warning("Search on provider %s timed out", prov.name)
            return None
        except MusicAssistantError as err:
            self.logger.warning("Search on provider %s failed: %s", prov.name, str(err))
            return None
        except Exception as err:
            self.logger.error("Search on provider %s failed: %s", prov.name, str(err), exc_info=err)
            return None
        # only successful results are cached, so failed or timed out
        # provider searches are simply retried on a next search
        await self._cache_search_results(
            cache_key,
            result,
            # plugin providers do not declare is_streaming_provider,
            # treat them as local so their results only get the short expiration
            SEARCH_CACHE_EXPIRATION_STREAMING_PROVIDER
            if getattr(prov, "is_streaming_provider", False)
            else SEARCH_CACHE_EXPIRATION_LOCAL_PROVIDER,
            prov.instance_id,
        )
        return result

    async def _cache_search_results(
        self, cache_key: str, result: SearchResults, expiration: int, provider: str
    ) -> None:
        """Store search results in the cache, logging (instead of raising) any cache errors."""
        try:
            await self.mass.cache.set(
                key=cache_key,
                data=result.to_dict(),
                expiration=expiration,
                provider=provider,
                category=CACHE_CATEGORY_SEARCH_RESULTS,
            )
        except Exception as err:
            self.logger.warning("Failed to cache search results for %s: %s", provider, str(err))

    def _get_covered_media_types(
        self, library_results: SearchResults, search_query: str
    ) -> set[tuple[MediaType, str]]:
        """
        Return the (media_type, provider domain/instance) pairs covered by the library.

        A pair is considered covered when the library holds a (near) exact name match
        for the search query that is mapped to that provider.
        """
        covered: set[tuple[MediaType, str]] = set()
        # extract the artist and title part in case the
        # query is formatted as "artist - title"
        if " - " in search_query:
            artist_part, title_part = search_query.split(" - ", 1)
        else:
            artist_part, title_part = None, search_query
        items: Sequence[MediaItemType | ItemMapping]
        for items in (
            library_results.artists,
            library_results.albums,
            library_results.tracks,
            library_results.playlists,
            library_results.radio,
            library_results.audiobooks,
            library_results.podcasts,
        ):
            for item in items:
                if compare_strings(item.name, search_query, strict=False):
                    pass
                elif artist_part and compare_strings(item.name, title_part, strict=False):
                    # the item name matches the title part only,
                    # so the artist part must match one of the item artists
                    if not any(
                        compare_strings(artist.name, artist_part, strict=False)
                        for artist in getattr(item, "artists", [])
                    ):
                        continue
                else:
                    continue
                for prov_mapping in cast("MediaItemType", item).provider_mappings:
                    if not prov_mapping.available:
                        continue
                    covered.add((item.media_type, prov_mapping.provider_domain))
                    covered.add((item.media_type, prov_mapping.provider_instance))
        return covered

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

    async def _get_provider_sound_effects(self, provider: MusicProvider) -> list[SoundEffect]:
        """Return all sound effect items from a single provider."""
        try:
            return [item async for item in provider.get_sound_effects()]
        except Exception as err:
            self.logger.warning(
                "Error while fetching sound effects from %s: %s",
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
                    # suppress per-item events during sync; a large library would otherwise
                    # emit one (serialized per client) for every item. Subscribers refresh
                    # on MUSIC_SYNC_COMPLETED and track progress via TASKS_UPDATED instead.
                    token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
                    try:
                        await provider.sync_library(media_type)
                    finally:
                        SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)
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
            return "sync_provider_artists"
        if media_type == MediaType.ALBUM:
            return "sync_provider_albums"
        if media_type == MediaType.TRACK:
            return "sync_provider_tracks"
        if media_type == MediaType.PLAYLIST:
            return "sync_provider_playlists"
        if media_type == MediaType.RADIO:
            return "sync_provider_radios"
        if media_type == MediaType.AUDIOBOOK:
            return "sync_provider_audiobooks"
        if media_type == MediaType.PODCAST:
            return "sync_provider_podcasts"
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
        # freshly synced content is the only source of new duplicates, so the reconciliation
        # pass owes the library another walk; it starts once the current one reaches the end,
        # since rewinding right now would keep re-examining the same prefix forever
        self._set_track_reconciliation_state(self._track_reconciliation_cursor, True)
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
            translation_key="database_cleanup",
            translation_owner=self.translation_owner,
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
            translation_key="correct_provider_mappings",
            translation_owner=self.translation_owner,
            metadata={
                "task_domain": "music_provider_mapping_correction",
            },
            allow_retry=True,
        )

    def _register_track_reconciliation_task(self) -> BackgroundTask:
        """Register the recurring duplicate track reconciliation background task."""
        # runs every hour rather than spread across the day: it is bounded to a small
        # batch of candidates per run and never leaves the local database
        return self.mass.tasks.register_scheduled_task(
            task_id=TRACK_RECONCILIATION_TASK_ID,
            name="Reconcile duplicate tracks",
            handler=self._reconcile_duplicate_tracks,
            schedule=TaskSchedule.hourly(),
            translation_key="reconcile_duplicate_tracks",
            translation_owner=self.translation_owner,
            metadata={
                "task_domain": "music_track_reconciliation",
            },
            allow_retry=True,
        )

    async def _reconcile_duplicate_tracks(self) -> None:
        """Merge a small batch of library tracks that are held twice across providers."""
        if self.active_sync_tasks:
            # a sync is still filling in albums and mappings, so hold off rather than
            # judge duplicates against a half-populated library
            update_current_task_progress_text("Waiting for music sync completion")
            return
        self._start_next_pass_if_due()
        if (cursor := self._track_reconciliation_cursor) is None:
            # the library has been walked end to end and nothing has been synced since,
            # so there is nothing to look for: skip the query rather than scan for a miss
            update_current_task_progress_text("No duplicate tracks found")
            return
        update_current_task_progress_text("Searching for duplicate tracks")
        rows = await self.database.get_rows_from_query(
            _DUPLICATE_TRACK_CANDIDATES_QUERY,
            {
                "max_duration_delta": TRACK_RECONCILIATION_MAX_DURATION_DELTA,
                "cursor_item_id_1": cursor[0],
                "cursor_item_id_2": cursor[1],
            },
            limit=TRACK_RECONCILIATION_BATCH_SIZE,
        )
        if not rows:
            self._set_track_reconciliation_state(None, self._track_reconciliation_rescan_due)
            update_current_task_progress_text("No duplicate tracks found")
            return
        merged = 0
        retry_due = False
        examined = cursor
        try:
            for index, row in enumerate(rows, 1):
                update_current_task_progress_from_index(
                    index, len(rows), f"Checking duplicate track {index}/{len(rows)}"
                )
                try:
                    if await self._merge_duplicate_track_pair(
                        int(row["item_id_1"]), int(row["item_id_2"])
                    ):
                        merged += 1
                except MediaNotFoundError:
                    # an earlier merge in this batch already absorbed one of the two rows
                    pass
                except MusicAssistantError as err:
                    # a pair that failed on something transient deserves another look
                    retry_due = True
                    report_current_task_failure(str(err))
                    self.logger.warning(
                        "Error while reconciling duplicate tracks %s and %s: %s",
                        row["item_id_1"],
                        row["item_id_2"],
                        str(err),
                        exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
                    )
                examined = (int(row["item_id_1"]), int(row["item_id_2"]))
        finally:
            # resume after the pair examined last, so candidates this run refused can never
            # starve the ones behind them, not even a further pair of the same track that the
            # batch boundary cut off. Recording it even when the run is cut short keeps the
            # pairs it did not reach for the next run rather than skipping past them.
            walked_to_end = len(rows) < TRACK_RECONCILIATION_BATCH_SIZE and examined == (
                int(rows[-1]["item_id_1"]),
                int(rows[-1]["item_id_2"]),
            )
            # a merge moves album and artist relations onto the surviving row, which can make
            # it a duplicate of a row this walk has already passed, so ask for another pass
            self._set_track_reconciliation_state(
                None if walked_to_end else examined,
                self._track_reconciliation_rescan_due or merged > 0 or retry_due,
            )
        update_current_task_progress(100, f"Merged {merged} duplicate track(s)")

    def _restore_track_reconciliation_state(self) -> None:
        """Pick the duplicate track walk back up where the previous run left it."""
        cursor = self.mass.config.get_raw_core_config_value(
            self.domain, CONF_TRACK_RECONCILIATION_CURSOR, [0, 0]
        )
        self._track_reconciliation_cursor = (
            (int(cursor[0]), int(cursor[1])) if len(cursor) == 2 else None
        )
        self._track_reconciliation_rescan_due = bool(
            self.mass.config.get_raw_core_config_value(
                self.domain, CONF_TRACK_RECONCILIATION_RESCAN_DUE, False
            )
        )

    def _set_track_reconciliation_state(
        self, cursor: tuple[int, int] | None, rescan_due: bool
    ) -> None:
        """
        Record how far the duplicate track walk has come, surviving a restart.

        :param cursor: The pair examined last, or None once the walk reached the end.
        :param rescan_due: Whether a completed sync still owes the library another pass.
        """
        self._track_reconciliation_cursor = cursor
        self._track_reconciliation_rescan_due = rescan_due
        self.mass.config.set_raw_core_config_value(
            self.domain, CONF_TRACK_RECONCILIATION_CURSOR, list(cursor) if cursor else []
        )
        self.mass.config.set_raw_core_config_value(
            self.domain, CONF_TRACK_RECONCILIATION_RESCAN_DUE, rescan_due
        )

    def _start_next_pass_if_due(self) -> None:
        """Rewind the duplicate track walk if a sync has added content and the walk is done."""
        # rewinding a walk still in progress would keep re-examining the same first
        # candidates, so a pending rescan waits for the current one to reach the end
        if not self._track_reconciliation_rescan_due:
            return
        if self._track_reconciliation_cursor is not None:
            return
        self._set_track_reconciliation_state((0, 0), False)

    async def _albums_agree_on_edition(self, item_id_1: int, item_id_2: int) -> bool:
        """
        Check that two tracks share an album whose edition matches as well as its title.

        :param item_id_1: Library ID of the first track.
        :param item_id_2: Library ID of the second track.
        """
        # the query relates titles loosely so a spelled-out retail suffix cannot hide a
        # shared album, which leaves the identity for the album comparison to confirm. An
        # edition is held apart from the title: without that an original and its remaster or
        # deluxe edition look like the same album whenever neither track carries a version
        rows = await self.database.get_rows_from_query(
            _SHARED_ALBUM_EDITIONS_QUERY,
            {"item_id_1": item_id_1, "item_id_2": item_id_2},
        )
        return any(
            compare_album_name(row["name_1"], row["name_2"])
            and compare_version(row["version_1"], row["version_2"])
            for row in rows
        )

    async def _merge_duplicate_track_pair(self, item_id_1: int, item_id_2: int) -> bool:
        """
        Merge two candidate rows if they are confirmed to be the same track.

        :param item_id_1: Library ID of the lower-numbered candidate row.
        :param item_id_2: Library ID of the higher-numbered candidate row.
        :return: True when the rows were merged, False when they were left alone.
        """
        track_1 = await self.tracks.get_library_item(item_id_1)
        track_2 = await self.tracks.get_library_item(item_id_2)
        # the checks below establish that both rows sit at the same position on an equally
        # titled album, which is the album agreement strict mode looks for, so the remaining
        # check is run in non-strict mode. Its version check is reinstated here
        # explicitly: without it a remaster, remix or radio edit of equal length would be
        # accepted as the original.
        if not compare_version(track_1.version, track_2.version):
            return False
        if not await self._albums_agree_on_edition(item_id_1, item_id_2):
            return False
        if not compare_track(track_1, track_2, strict=False):
            return False
        # keep the row that carries the most provider mappings so the fewest mappings and
        # relations have to move; equal counts keep the oldest row, which the query orders first
        target, source = (
            (track_1, track_2)
            if len(track_1.provider_mappings) >= len(track_2.provider_mappings)
            else (track_2, track_1)
        )
        self.logger.debug(
            "Merging duplicate track %s (id %s) into id %s",
            target.name,
            source.item_id,
            target.item_id,
        )
        await self.tracks.merge_library_items(target.item_id, source.item_id)
        return True

    def _queue_database_cleanup_task(self) -> BackgroundTask:
        """Queue the post-sync database cleanup as a managed background task."""
        self._register_database_cleanup_task()
        return self.mass.tasks.run_task(DATABASE_CLEANUP_TASK_ID)

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
            initial_delay=INITIAL_SYNC_DELAY if is_initial else None,
            translation_key=self._get_sync_task_translation_key(media_type),
            translation_args=[provider.name],
            translation_owner=self.translation_owner,
            metadata=self._get_sync_task_metadata(provider, media_type),
            allow_retry=True,
        )

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

    async def _resolve_playlog_item(self, media_item: MediaItemType | ItemMapping) -> MediaItemType:
        """
        Return the full media item for a (possibly minimized) media item reference.

        :param media_item: The media item to resolve, either full or an ItemMapping.
        """
        if not isinstance(media_item, ItemMapping):
            return media_item
        resolved = await self.get_item(
            media_item.media_type,
            media_item.item_id,
            media_item.provider,
            allow_update_metadata=False,
        )
        if isinstance(resolved, BrowseFolder):
            msg = f"{media_item.uri} does not resolve to a media item"
            raise MediaNotFoundError(msg)
        return resolved

    async def _upsert_playlog(self, entry: dict[str, Any]) -> None:
        """
        Write a playlog row, updating the existing row for the item/user if there is one.

        Columns left out of the entry keep whatever the existing row holds, and
        `user_initiated` is sticky: once a play was explicitly user-initiated it stays that
        way for the lifetime of the row, so a later side-effect credit (an autoplay replay,
        or a track crediting its album/artist) can never demote it and drop the item out of
        the "recently played" recommendations.

        The generic `database.upsert()` cannot express either half of that: the sticky OR is
        playlog-specific, and it needs an explicit conflict target because the playlog carries
        more than one unique constraint.

        :param entry: The playlog column values to write, including all of
            `PLAYLOG_CONFLICT_KEYS`.
        """
        columns = list(entry)
        updates = [
            f"user_initiated = {DB_TABLE_PLAYLOG}.user_initiated OR excluded.user_initiated"
            if column == "user_initiated"
            else f"{column} = excluded.{column}"
            for column in columns
            if column not in PLAYLOG_CONFLICT_KEYS
        ]
        await self.database.execute_write(
            f"INSERT INTO {DB_TABLE_PLAYLOG} ({', '.join(columns)}) "
            f"VALUES ({', '.join(f':{column}' for column in columns)}) "
            f"ON CONFLICT({', '.join(PLAYLOG_CONFLICT_KEYS)}) DO UPDATE SET {', '.join(updates)}",
            entry,
        )

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
                await self._upsert_playlog(playlog_entry)

    async def _credit_podcast_play(
        self,
        podcast: Podcast | ItemMapping,
        *,
        timestamp: float,
        user_ids: list[str],
        queue_id: str | None,
    ) -> None:
        """Credit the parent podcast with a play so the show surfaces in recently played."""
        # Resolve to the library item first, like _credit_artist_plays does, so an episode's
        # parent-podcast credit lands on the same library-scoped row as an explicit play of the
        # library show, instead of creating a separate provider-scoped duplicate.
        db_podcast = await self.podcasts.get_library_item_by_prov_id(
            podcast.item_id, podcast.provider
        )
        credited_podcast: Podcast | ItemMapping = db_podcast if db_podcast else podcast
        playlog_entry: dict[str, Any] = {
            "item_id": credited_podcast.item_id,
            "provider": "library" if db_podcast else podcast.provider,
            "media_type": MediaType.PODCAST.value,
            "name": credited_podcast.name,
            "image": serialize_to_json(credited_podcast.image.to_dict())
            if credited_podcast.image
            else None,
            "fully_played": True,
            "seconds_played": None,
            "timestamp": timestamp,
            "queue_id": queue_id,
            "user_initiated": False,
        }
        for user_id in user_ids:
            playlog_entry["userid"] = user_id
            await self._upsert_playlog(playlog_entry)

    async def _get_item_by_name(
        self,
        name: str,
        artist: str | None = None,
        album: str | None = None,
        media_type: MediaType | None = None,
    ) -> MediaItemType | ItemMapping | None:
        """Try to find a media item (such as a playlist) by name."""
        # Future todo: enhance this method with AI capabilities to allow typos and
        # natural language.
        searchname = name.lower()
        allowed_media_types = [
            MediaType.PLAYLIST,
            MediaType.RADIO,
            MediaType.TRACK,
            MediaType.ALBUM,
            MediaType.ARTIST,
            MediaType.AUDIOBOOK,
            MediaType.PODCAST,
        ]
        if media_type in (None, MediaType.UNKNOWN):
            media_types = allowed_media_types
        elif media_type not in allowed_media_types:
            raise InvalidDataError(
                f"{media_type} is not a supported media_type. "
                f"Supported media_types are {allowed_media_types}"
            )
        else:
            media_types = [media_type]
        library_functions = [
            self.get_controller(media_type).library_items for media_type in media_types
        ]
        # prefer (exact) lookup in the library by name
        for func in library_functions:
            result = await func(search=searchname)
            for item in result:
                # handle optional artist filter
                if (
                    artist
                    and (artists := getattr(item, "artists", None))
                    and not any(x for x in artists if x.name.lower() == artist.lower())
                ):
                    continue
                # handle optional album filter
                if (
                    album
                    and (item_album := getattr(item, "album", None))
                    and item_album.name.lower() != album.lower()
                ):
                    continue
                if searchname == item.name.lower():
                    return item
        # nothing found in the library, fallback to global search
        search_name = name
        if album and artist:
            search_name = f"{artist} - {album} - {name}"
        elif album:
            search_name = f"{album} - {name}"
        elif artist:
            search_name = f"{artist} - {name}"
        search_results = await self.search(
            search_query=search_name,
            media_types=[media_type]
            if media_type and media_type != MediaType.UNKNOWN
            else MediaType.ALL,
            limit=8,
        )
        for results in (
            search_results.tracks,
            search_results.albums,
            search_results.playlists,
            search_results.artists,
            search_results.radio,
            search_results.audiobooks,
            search_results.podcasts,
        ):
            for _item in results:
                # simply return the first item because search is already sorted by best match
                return _item
        return None

    async def _handle_verify_item_uri(self, uri: str) -> bool:
        user = get_current_user()

        try:
            media_type, provider_instance_id_or_domain, item_id = await parse_uri(uri)
        except InvalidProviderURI, InvalidProviderID:
            return False

        # fast return for a provider uri which is not part of a user with a provider filter
        if (
            provider_instance_id_or_domain != "library"
            and user
            and user.provider_filter
            and provider_instance_id_or_domain not in user.provider_filter
        ):
            return False

        # verify that item itself exists
        try:
            item = await self.get_item(
                media_type=media_type,
                item_id=item_id,
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                allow_update_metadata=False,  # no need trigger more methods
            )
        except MediaNotFoundError, NotImplementedError:
            # NotImplementedError: the uri has a valid format, but specifies an unknown media type
            return False

        # non library item handling for users with no filter, or no user at all
        if (
            provider_instance_id_or_domain != "library"
            or not user
            or (user and not user.provider_filter)
            or isinstance(item, BrowseFolder)
        ):
            return True

        # library item handling for users with provider filter
        for provider_mapping in item.provider_mappings:
            if provider_mapping.provider_instance in user.provider_filter:
                return True

        return False
