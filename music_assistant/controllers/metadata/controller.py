"""All logic for metadata retrieval."""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from time import time
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, uuid5

import aiohttp
from music_assistant_models.auth import Scope
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    EventType,
    ExternalID,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import Album, Artist, BrowseFolder, Track

from music_assistant.constants import (
    CONF_LANGUAGE,
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACKS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import system_auth_context
from music_assistant.helpers.api import api_command
from music_assistant.helpers.compare import (
    ALBUM_RETAIL_SUFFIX_KEYS,
    album_retail_suffix_sql_match,
)
from music_assistant.helpers.images import cleanup_thumb_cache
from music_assistant.helpers.lyrics import extract_lrc_lyrics, normalize_lrc_lyrics
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.helpers.util import try_parse_int
from music_assistant.models.core_controller import CoreController
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ALBUM_RECONCILIATION_TASK_ID,
    CONF_ENABLE_ONLINE_METADATA,
    CONF_ENABLE_RADIO_METADATA_LOOKUP,
    CONF_LINK_PROVIDERS_VIA_MUSICBRAINZ,
    CONF_MUSICBRAINZ_LINKED_DOMAINS,
    CONF_PREFER_LOCAL_GENRES,
    CONF_THUMB_CACHE_MAX_SIZE,
    DEFAULT_LANGUAGE,
    DEFAULT_THUMB_CACHE_MAX_SIZE_MB,
    LOCALES,
    METADATA_LOOKUP_TASK_ID_PREFIX,
    METADATA_SCAN_BATCH_SIZE,
    MISSING_METADATA_SCAN_TASK_ID,
    MUSICBRAINZ_LINK_BATCH_SIZE,
    MUSICBRAINZ_LINK_DOMAINS,
    MUSICBRAINZ_LINK_ITEM_INTERVAL,
    MUSICBRAINZ_LINK_ITEM_TIMEOUT,
    MUSICBRAINZ_LINK_TASK_ID,
    PLAYLIST_METADATA_SCAN_TASK_ID,
    REFRESH_INTERVAL,
    THUMB_CACHE_CLEANUP_TASK_ID,
)
from .enrichment import MetadataEnrichmentMixin
from .images import ImageProxyMixin
from .radio import RadioArtworkMixin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.event import MassEvent
    from music_assistant_models.media_items import Audiobook, MediaItemType, Playlist, Podcast

    from music_assistant import MusicAssistant
    from music_assistant.controllers.music.media.base import MediaControllerBase
    from music_assistant.helpers.json import SerializableType
    from music_assistant.models.metadata_provider import MetadataProvider

# the errors one item of a maintenance scan may fail with without ending the scan
_SCAN_ITEM_ERRORS = (MusicAssistantError, aiohttp.ClientError, TimeoutError)


@dataclass(frozen=True, slots=True)
class _LinkPhase:
    """One selection of library items a MusicBrainz link run works through."""

    name: str
    controller: MediaControllerBase[Any]
    table: str
    query: str
    params: dict[str, Any]


class MetaDataController(
    ImageProxyMixin, RadioArtworkMixin, MetadataEnrichmentMixin, CoreController
):
    """Controller that handles metadata retrieval and management for media items."""

    domain: str = "metadata"
    config: CoreConfig

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        self.cache = self.mass.cache
        self._pref_lang: str | None = None
        self.manifest.name = "Metadata controller"
        self.manifest.description = (
            "Music Assistant's core controller which handles all metadata for music."
        )
        self.manifest.icon = "book-information-variant"
        self._throttler = Throttler(1, 30)
        # image-id bookkeeping, all bounded by _IMAGE_ID_LRU_MAX and sharing the
        # same key/id string objects so the combined footprint stays small:
        # - _image_id_forward: (provider, path) -> image_id memo so serializing a
        #   known image skips the sha256 and the lock entirely. Read lock-free
        #   (single dict lookup is atomic), mutated only while holding the lock.
        # - _image_id_lru: image_id -> (provider, path). Write-through hot cache
        #   in front of the cache controller so that resolving an image by id
        #   never blocks on sqlite if the URL was generated recently.
        # - _image_id_persisted: image_id -> epoch of the last persist to the
        #   cache db, so repeat encounters skip the sqlite write.
        # The lock is needed because compute_image_id() runs from the executor
        # thread during outbound websocket serialization.
        self._image_id_forward: dict[tuple[str, str], str] = {}
        self._image_id_lru: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._image_id_persisted: dict[str, float] = {}
        self._image_id_lock = threading.Lock()
        # corrupt metadata rows found by the last scan pass, per table, for diagnostics
        self._corrupt_metadata_rows: dict[str, list[dict[str, str | int]]] = {}
        # what the last MusicBrainz link run did, for diagnostics
        self._musicbrainz_link_summary: dict[str, SerializableType] | None = None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        return (
            # deliberately without a default_value: only values differing from the entry default
            # are persisted, so declaring one would make a chosen DEFAULT_LANGUAGE
            # indistinguishable from "never chosen". The locale property applies it on read.
            ConfigEntry(
                key=CONF_LANGUAGE,
                type=ConfigEntryType.STRING,
                required=False,
                options=[ConfigValueOption(key, title=value) for key, value in LOCALES.items()],
            ),
            ConfigEntry(
                key=CONF_ENABLE_ONLINE_METADATA,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_LINK_PROVIDERS_VIA_MUSICBRAINZ,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=True,
                advanced=True,
            ),
            # state of the MusicBrainz link run; declared so a config save carries it over
            ConfigEntry(
                key=CONF_MUSICBRAINZ_LINKED_DOMAINS,
                type=ConfigEntryType.STRING,
                required=False,
                multi_value=True,
                hidden=True,
            ),
            ConfigEntry(
                key=CONF_PREFER_LOCAL_GENRES,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=False,
            ),
            ConfigEntry(
                key=CONF_ENABLE_RADIO_METADATA_LOOKUP,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_THUMB_CACHE_MAX_SIZE,
                type=ConfigEntryType.INTEGER,
                required=False,
                default_value=DEFAULT_THUMB_CACHE_MAX_SIZE_MB,
                range=(50, 5000),
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self.config = config
        if not self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            # silence PIL logger
            logging.getLogger("PIL").setLevel(logging.WARNING)

    async def post_setup(self) -> None:
        """Handle logic after all core controllers have been set up."""
        # canonical opaque-id endpoint, served by both the public webserver
        # and the streams server (the latter is what player metadata URLs hit)
        self.mass.streams.register_dynamic_route("/imageproxy/*", self.handle_imageproxy)
        self.mass.webserver.register_dynamic_route("/imageproxy/*", self.handle_imageproxy)
        self._register_maintenance_tasks()
        self.mass.subscribe(self._on_music_sync_completed, EventType.MUSIC_SYNC_COMPLETED)

    async def close(self) -> None:
        """Handle logic on server stop."""
        self.mass.streams.unregister_dynamic_route("/imageproxy/*")
        self.mass.webserver.unregister_dynamic_route("/imageproxy/*")

    @property
    def providers(self) -> list[MetadataProvider]:
        """Return all loaded/running MetadataProviders."""
        return sorted(
            cast("list[MetadataProvider]", self.mass.get_providers(ProviderType.METADATA)),
            key=lambda p: p.priority,
        )

    @property
    def preferred_language(self) -> str:
        """Return preferred language for metadata (as 2 letter language code 'en')."""
        return self.locale.split("_")[0]

    @property
    def locale(self) -> str:
        """Return preferred language for metadata (as full locale code 'en_EN')."""
        value = self.mass.config.get_raw_core_config_value(
            self.domain, CONF_LANGUAGE, DEFAULT_LANGUAGE
        )
        return str(value)

    @property
    def link_providers_via_musicbrainz(self) -> bool:
        """Whether library items are linked to the other music providers through MusicBrainz."""
        return bool(self.config.get_value(CONF_LINK_PROVIDERS_VIA_MUSICBRAINZ))

    @api_command("metadata/set_default_preferred_language", required_scope=Scope.CONFIG_CORE_WRITE)
    def set_default_preferred_language(self, lang: str) -> None:
        """
        Set the default preferred language.

        Reasoning behind this is that the backend can not make a wise choice for the default,
        so relies on some external source that knows better to set this info, like the frontend
        or a streaming provider.
        Can only be set once (by this call or the user).
        """
        if self.mass.config.get_raw_core_config_value(self.domain, CONF_LANGUAGE):
            return  # already set
        self.set_preferred_language(lang)

    @api_command("metadata/set_preferred_language", required_scope=Scope.LIBRARY_MANAGE)
    def set_preferred_language(self, lang: str) -> None:
        """
        Set the preferred language.

        Note that this will not modify any existing metadata,
        but will be used for future lookups.
        """
        # prefer exact match
        if lang in LOCALES:
            self.mass.config.set_raw_core_config_value(self.domain, CONF_LANGUAGE, lang)
            return
        # try strict matching on either locale code or region
        lang = lang.lower().replace("-", "_")
        for locale_code, lang_name in LOCALES.items():
            if lang in (locale_code.lower(), lang_name.lower()):
                self.mass.config.set_raw_core_config_value(self.domain, CONF_LANGUAGE, locale_code)
                return
        # attempt loose match on language code or region code
        for lang_part in (lang[:2], lang[:-2]):
            for locale_code in tuple(LOCALES):
                language_code, region_code = locale_code.lower().split("_", 1)
                if lang_part in (language_code, region_code):
                    self.mass.config.set_raw_core_config_value(
                        self.domain, CONF_LANGUAGE, locale_code
                    )
                    return
        # if we reach this point, we couldn't match the language
        self.logger.warning("%s is not a valid language", lang)

    @api_command("metadata/update_metadata", required_scope=Scope.LIBRARY_MANAGE)
    async def update_metadata(
        self, item: str | MediaItemType, force_refresh: bool = False
    ) -> MediaItemType:
        """Get/update extra/enhanced metadata for/on given MediaItem."""
        async with self.cache.handle_refresh(force_refresh):
            if isinstance(item, str):
                retrieved_item = await self.mass.music.get_item_by_uri(item)
                if isinstance(retrieved_item, BrowseFolder):
                    raise TypeError("Cannot update metadata on a BrowseFolder item.")
                item = retrieved_item

        if item.provider != "library":
            # this shouldn't happen but just in case.
            raise RuntimeError("Metadata can only be updated for library items")

        async with self._throttler:
            # the refresh fills the household's library item from all its sources
            with system_auth_context():
                if item.media_type == MediaType.ARTIST:
                    await self._update_artist_metadata(
                        cast("Artist", item), force_refresh=force_refresh
                    )
                if item.media_type == MediaType.ALBUM:
                    await self._update_album_metadata(
                        cast("Album", item), force_refresh=force_refresh
                    )
                if item.media_type == MediaType.TRACK:
                    await self._update_track_metadata(
                        cast("Track", item), force_refresh=force_refresh
                    )
                if item.media_type == MediaType.PLAYLIST:
                    await self._update_playlist_metadata(
                        cast("Playlist", item), force_refresh=force_refresh
                    )
                if item.media_type == MediaType.AUDIOBOOK:
                    await self._update_audiobook_metadata(
                        cast("Audiobook", item), force_refresh=force_refresh
                    )
                if item.media_type == MediaType.PODCAST:
                    await self._update_podcast_metadata(
                        cast("Podcast", item), force_refresh=force_refresh
                    )
        return item

    def schedule_update_metadata(self, item: MediaItemType) -> None:
        """Schedule metadata update for given MediaItem."""
        if item.provider != "library":
            # this shouldn't happen but just in case.
            return
        last_refresh = item.metadata.last_refresh or 0
        needs_update = (time() - last_refresh) > REFRESH_INTERVAL
        if not needs_update:
            return
        assert item.uri is not None
        task_id = self._get_metadata_lookup_task_id(item.uri)
        _item = item

        self.mass.tasks.run_background_task(
            task_id=task_id,
            name=f"Update metadata for {item.name}",
            handler=lambda: self.update_metadata(_item),
            translation_key="update_metadata",
            translation_args=[item.name],
            translation_owner=self.translation_owner,
            metadata={
                "task_domain": "metadata_lookup",
                "item_uri": item.uri,
            },
        )

    async def link_item_to_musicbrainz(self, item: Artist | Album | Track) -> None:
        """
        Identify a library item on MusicBrainz and link it to the music providers known there.

        Only the item's identifiers and provider links are filled in; the full metadata
        refresh is left to :meth:`update_metadata`.

        :param item: The library artist, album or track.
        """
        if isinstance(item, Artist):
            if not item.mbid and (mbid := await self._get_artist_mbid(item)):
                item.mbid = mbid
            await self._link_artist_to_musicbrainz(item)
            await self.mass.music.artists.update_item_in_library(item.item_id, item)
        elif isinstance(item, Album):
            await self._link_album_to_musicbrainz(item)
            await self.mass.music.albums.update_item_in_library(item.item_id, item)
        else:
            await self._link_track_to_musicbrainz(item)
            await self.mass.music.tracks.update_item_in_library(item.item_id, item)

    @api_command(
        "metadata/get_track_lyrics", required_scope=Scope.LIBRARY_READ, allow_impersonation=True
    )
    async def get_track_lyrics(
        self,
        track: Track,
    ) -> tuple[str | None, str | None]:
        """
        Get lyrics for given track from metadata providers.

        Returns a tuple of (lyrics, lrc_lyrics) if found.
        """
        lyrics, lrc_lyrics = await self._get_track_lyrics(track)
        # on-demand lookups are not stored in the library db, so normalize on the way out
        # promoting LRC formatted text stored in the plain lyrics tag
        return lyrics, normalize_lrc_lyrics(lrc_lyrics or extract_lrc_lyrics(lyrics))

    async def get_diagnostics(self) -> dict[str, SerializableType] | None:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        diagnostics: dict[str, SerializableType] = {
            "musicbrainz_link": {
                "last_run": self._musicbrainz_link_summary,
                "pending": await self._pending_musicbrainz_link_counts(),
            }
        }
        if self._corrupt_metadata_rows:
            diagnostics["corrupt_metadata_rows"] = cast(
                "SerializableType", self._corrupt_metadata_rows
            )
        return diagnostics

    async def _get_track_lyrics(
        self,
        track: Track,
    ) -> tuple[str | None, str | None]:
        """Look up (lyrics, lrc_lyrics) for the given track."""
        if track.metadata and track.metadata.lyrics:
            return track.metadata.lyrics, track.metadata.lrc_lyrics

        if track.provider == "library":
            # the stored item is refreshed, never the caller's copy of it
            track = await self.mass.music.tracks.get(
                track.item_id, "library", allow_update_metadata=False
            )
            await self._update_track_metadata(track, force_refresh=False)
            return track.metadata.lyrics, track.metadata.lrc_lyrics

        # prefer lyrics from the track's own provider
        track_provider = self.mass.music.get_visible_provider(track.provider)
        if (
            isinstance(track_provider, MusicProvider)
            and ProviderFeature.LYRICS in track_provider.supported_features
        ):
            full_track = await self.mass.music.tracks.get_provider_item(
                track.item_id, track.provider
            )
            if full_track.metadata and full_track.metadata.lyrics:
                return full_track.metadata.lyrics, full_track.metadata.lrc_lyrics

        # fallback to other metadata providers
        for provider in self.providers:
            if ProviderFeature.LYRICS not in provider.supported_features:
                continue
            try:
                metadata = await provider.get_track_metadata(track)
            except Exception as err:
                # a provider failure must not abort the lookup — skip to the next provider
                self.logger.warning(
                    "Error fetching lyrics for %s from provider %s: %s",
                    track.name,
                    provider.name,
                    err,
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
                continue
            if metadata and (metadata.lyrics or metadata.lrc_lyrics):
                return metadata.lyrics, metadata.lrc_lyrics
        return None, None

    def _register_maintenance_tasks(self) -> None:
        """Register the recurring metadata maintenance background tasks."""
        # Spread across the full day so instances don't all hit the shared MusicBrainz mirror at once
        utc_hour, utc_minute = divmod(random.randint(0, 24 * 60 - 1), 60)
        desired_schedule = TaskSchedule.daily(hour=utc_hour, minute=utc_minute)
        self.mass.tasks.register_scheduled_task(
            task_id=MISSING_METADATA_SCAN_TASK_ID,
            name="Scan missing metadata",
            handler=self._scan_missing_metadata,
            schedule=desired_schedule,
            translation_key="scan_missing_metadata",
            translation_owner=self.translation_owner,
            # the task domain keeps its historical artist-only name along with the task id
            metadata={"task_domain": "metadata_missing_artist_metadata_scan"},
            allow_retry=True,
        )
        self.mass.tasks.register_scheduled_task(
            task_id=PLAYLIST_METADATA_SCAN_TASK_ID,
            name="Refresh playlist metadata",
            handler=self._refresh_playlist_metadata_batch,
            schedule=desired_schedule,
            translation_key="refresh_playlist_metadata",
            translation_owner=self.translation_owner,
            metadata={"task_domain": "metadata_playlist_metadata_scan"},
            allow_retry=True,
        )
        self.mass.tasks.register_scheduled_task(
            task_id=THUMB_CACHE_CLEANUP_TASK_ID,
            name="Cleanup thumbnail cache",
            handler=self._cleanup_thumb_cache,
            schedule=desired_schedule,
            translation_key="cleanup_thumbnail_cache",
            translation_owner=self.translation_owner,
            metadata={"task_domain": "metadata_thumb_cache_cleanup"},
            allow_retry=True,
        )
        # runs every hour rather than spread across the day: it is bounded to a small
        # batch of albums per run, so there is no shared-mirror stampede to avoid
        self.mass.tasks.register_scheduled_task(
            task_id=ALBUM_RECONCILIATION_TASK_ID,
            name="Reconcile duplicate albums",
            handler=self._reconcile_duplicate_albums,
            schedule=TaskSchedule.hourly(),
            translation_key="reconcile_duplicate_albums",
            translation_owner=self.translation_owner,
            metadata={"task_domain": "metadata_album_reconciliation"},
            allow_retry=True,
        )
        # bounded to a small batch per run and paced, so it can run hourly as well
        self.mass.tasks.register_scheduled_task(
            task_id=MUSICBRAINZ_LINK_TASK_ID,
            name="Link library to MusicBrainz",
            handler=self._link_library_to_musicbrainz,
            schedule=TaskSchedule.hourly(),
            translation_key="link_library_to_musicbrainz",
            translation_owner=self.translation_owner,
            metadata={"task_domain": "metadata_musicbrainz_link"},
            allow_retry=True,
        )

    def _on_music_sync_completed(self, _event: MassEvent) -> None:
        """Queue the MusicBrainz link run for what a library sync has just added."""
        # a run already pending or running is not queued twice; the hourly schedule
        # covers whatever such a run misses
        self.mass.tasks.run_task(MUSICBRAINZ_LINK_TASK_ID)

    @staticmethod
    def _get_metadata_lookup_task_id(uri: str) -> str:
        """Return deterministic task id for a metadata lookup."""
        return f"{METADATA_LOOKUP_TASK_ID_PREFIX}_{uuid5(NAMESPACE_URL, uri).hex}"

    async def _scan_missing_metadata(self) -> None:
        """Collect the metadata of a batch of artists and albums that never had theirs collected."""
        update_current_task_progress_text("Searching for artists and albums with missing metadata")
        artists = await self._get_scan_batch(
            self.mass.music.artists,
            DB_TABLE_ARTISTS,
            _missing_metadata_query(DB_TABLE_ARTISTS, description=True),
        )
        albums = await self._get_scan_batch(
            self.mass.music.albums, DB_TABLE_ALBUMS, _missing_metadata_query(DB_TABLE_ALBUMS)
        )
        items: list[Artist | Album] = [*artists, *albums]
        if not items:
            update_current_task_progress_text("No artists or albums with missing metadata found")
            return
        skipped = 0
        for index, item in enumerate(items, 1):
            feature = (
                ProviderFeature.ARTIST_METADATA
                if isinstance(item, Artist)
                else ProviderFeature.ALBUM_METADATA
            )
            if self._metadata_rate_limited(feature):
                # the item stays never-refreshed, so the next run selects it again
                skipped += 1
                continue
            try:
                update_current_task_progress_from_index(
                    index,
                    len(items),
                    f"Refreshing metadata for {item.media_type.value} {index}/{len(items)}: "
                    f"{item.name}",
                )
                if isinstance(item, Artist):
                    await self._update_artist_metadata(item, force_refresh=False)
                else:
                    await self._update_album_metadata(item, force_refresh=False)
            except Exception as err:
                report_current_task_failure(f"{item.name}: {err}")
                self.logger.warning(
                    "Error while updating metadata for %s %s: %s",
                    item.media_type.value,
                    item.name,
                    str(err),
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
        summary = f"Processed {len(items) - skipped} item(s)"
        if skipped:
            summary += f", skipped {skipped} while a metadata provider is rate limiting"
            self.logger.debug("Missing metadata scan: %s", summary)
        update_current_task_progress(100, summary)

    async def _refresh_playlist_metadata_batch(self) -> None:
        """Refresh metadata for a small batch of library playlists."""
        update_current_task_progress_text("Searching for playlists needing metadata refresh")
        refresh_before = int(time() - REFRESH_INTERVAL)
        query = (
            f"{DB_TABLE_PLAYLISTS}.is_dynamic = 0 AND ("
            f"json_extract({DB_TABLE_PLAYLISTS}.metadata,'$.last_refresh') ISNULL "
            f"OR json_extract({DB_TABLE_PLAYLISTS}.metadata,'$.last_refresh') < {refresh_before})"
        )
        playlists = await self._get_scan_batch(self.mass.music.playlists, DB_TABLE_PLAYLISTS, query)
        if not playlists:
            update_current_task_progress_text("No playlists require metadata refresh")
            return
        for index, playlist in enumerate(playlists, 1):
            try:
                update_current_task_progress_from_index(
                    index,
                    len(playlists),
                    f"Refreshing playlist metadata {index}/{len(playlists)}: {playlist.name}",
                )
                await self._update_playlist_metadata(playlist, force_refresh=False)
            except Exception as err:
                report_current_task_failure(f"{playlist.name}: {err}")
                self.logger.warning(
                    "Error while refreshing playlist metadata for %s: %s",
                    playlist.name,
                    str(err),
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
        update_current_task_progress(100, f"Processed {len(playlists)} playlist(s)")

    async def _reconcile_duplicate_albums(self) -> None:
        """Enrich and re-match a small batch of sparse or possibly duplicated albums."""
        update_current_task_progress_text("Searching for albums needing reconciliation")
        # candidates are selected again once their refresh is due (sooner after a temporary
        # provider failure), rather than only ever once
        refresh_before = int(time() - REFRESH_INTERVAL)
        query = (
            f"({DB_TABLE_ALBUMS}.album_type = '{AlbumType.UNKNOWN.value}' "
            f"OR {_duplicate_album_sibling_guard()}) AND ("
            f"json_extract({DB_TABLE_ALBUMS}.metadata,'$.last_refresh') ISNULL "
            f"OR json_extract({DB_TABLE_ALBUMS}.metadata,'$.last_refresh') < {refresh_before})"
        )
        albums = await self._get_scan_batch(self.mass.music.albums, DB_TABLE_ALBUMS, query)
        if not albums:
            update_current_task_progress_text("No albums require reconciliation")
            return
        skipped = 0
        for index, album in enumerate(albums, 1):
            if self._metadata_rate_limited(ProviderFeature.ALBUM_METADATA):
                # the album stays untouched, so the next run selects it again
                skipped += 1
                continue
            try:
                update_current_task_progress_from_index(
                    index,
                    len(albums),
                    f"Reconciling album {index}/{len(albums)}: {album.name}",
                )
                # enrich sparse provider data (type/year/metadata) first so the follow-up
                # match has full album details to work with, then re-fetch the now-enriched
                # library row before re-matching: match_providers merges a confirmed mapping
                # into an existing duplicate through the safe add_provider_mappings path
                try:
                    await self._update_album_metadata(album, force_refresh=False)
                    reconciled_album = await self.mass.music.albums.get_library_item(album.item_id)
                except MediaNotFoundError:
                    # both rows of a duplicate pair can share a batch, so this row may
                    # already have been merged into its duplicate earlier in the run
                    continue
                await self.mass.music.albums.match_providers(reconciled_album)
            except _SCAN_ITEM_ERRORS as err:
                report_current_task_failure(f"{album.name}: {err}")
                self.logger.warning(
                    "Error while reconciling album %s: %s",
                    album.name,
                    str(err),
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
        summary = f"Processed {len(albums) - skipped} album(s)"
        if skipped:
            summary += f", skipped {skipped} while a metadata provider is rate limiting"
            self.logger.debug("Album reconciliation: %s", summary)
        update_current_task_progress(100, summary)

    async def _cleanup_thumb_cache(self) -> None:
        """Remove oldest thumbnails when the cache folder exceeds the configured limit."""
        max_size_mb = (
            try_parse_int(
                self.config.get_value(CONF_THUMB_CACHE_MAX_SIZE), DEFAULT_THUMB_CACHE_MAX_SIZE_MB
            )
            or DEFAULT_THUMB_CACHE_MAX_SIZE_MB
        )
        removed = await cleanup_thumb_cache(self.mass.cache_path, max_size_mb * 1024 * 1024)
        if removed:
            self.logger.debug("Thumbnail cache cleanup: removed %s file(s)", removed)

    async def _link_library_to_musicbrainz(self) -> None:
        """Identify a batch of library items on MusicBrainz and link them to the music providers."""
        if (musicbrainz := self._musicbrainz_provider()) is None:
            update_current_task_progress_text("The MusicBrainz provider is not loaded")
            return
        if not self.link_providers_via_musicbrainz:
            update_current_task_progress_text("Linking through MusicBrainz is disabled")
            return
        if self.mass.music.active_sync_tasks:
            # a sync is still adding items and their mappings, so the run waits for the
            # completed sync to queue it again
            update_current_task_progress_text("Waiting for music sync completion")
            return
        linked_domains = self._refresh_musicbrainz_linked_domains()
        processed: dict[str, int] = {}
        linked = not_found = failed = 0
        rate_limited = False
        budget = MUSICBRAINZ_LINK_BATCH_SIZE
        for phase in self._musicbrainz_link_phases(linked_domains):
            if budget <= 0 or rate_limited:
                break
            update_current_task_progress_text(f"Searching for {phase.name}")
            items: list[Artist | Album | Track] = await self._get_scan_batch(
                phase.controller,
                phase.table,
                phase.query,
                phase.params,
                limit=budget,
                order_by="timestamp_added_desc",
            )
            for item in items:
                if musicbrainz.rate_limited:
                    # the pacing already keeps this run within the limit, so a cooldown
                    # means the mirror is busy: leave the rest to the next run
                    update_current_task_progress_text(
                        "MusicBrainz is rate limiting, resuming next run"
                    )
                    rate_limited = True
                    break
                if processed:
                    # the pause between two items, so the run's first one starts right away
                    await asyncio.sleep(MUSICBRAINZ_LINK_ITEM_INTERVAL)
                update_current_task_progress_from_index(
                    MUSICBRAINZ_LINK_BATCH_SIZE - budget + 1,
                    MUSICBRAINZ_LINK_BATCH_SIZE,
                    f"Identifying {item.media_type.value} on MusicBrainz: {item.name}",
                )
                mapping_count = len(item.provider_mappings)
                try:
                    async with asyncio.timeout(MUSICBRAINZ_LINK_ITEM_TIMEOUT):
                        await self.link_item_to_musicbrainz(item)
                except _SCAN_ITEM_ERRORS as err:
                    failed += 1
                    report_current_task_failure(f"{item.name}: {err}")
                    self.logger.warning(
                        "Error while identifying %s %s on MusicBrainz: %s",
                        item.media_type.value,
                        item.name,
                        str(err),
                        exc_info=err if self.logger.isEnabledFor(10) else None,
                    )
                else:
                    if item.metadata.last_musicbrainz_lookup is None:
                        # the identity step logged why it gave up and left the marker unset,
                        # so the item heads the selection again next run
                        failed += 1
                        report_current_task_failure(f"{item.name}: lookup failed")
                    else:
                        linked += len(item.provider_mappings) > mapping_count
                        not_found += item.mbid is None
                processed[phase.name] = processed.get(phase.name, 0) + 1
                budget -= 1
        self._musicbrainz_link_summary = {
            "finished_at": int(time()),
            "processed": cast("SerializableType", processed),
            "linked": linked,
            "not_found": not_found,
            "failed": failed,
            "stopped_on_rate_limit": rate_limited,
        }
        self.logger.debug(
            "MusicBrainz link run processed %s, linked %d, not found %d, failed %d%s",
            ", ".join(f"{count} {name}" for name, count in processed.items()) or "nothing",
            linked,
            not_found,
            failed,
            ", stopped on rate limit" if rate_limited else "",
        )
        update_current_task_progress(100, f"Processed {sum(processed.values())} item(s)")

    def _musicbrainz_link_phases(self, linked_domains: dict[str, int]) -> list[_LinkPhase]:
        """
        Return the selections a MusicBrainz link run works through, in order.

        :param linked_domains: Since when each linked music provider has been loaded, by domain.
        """
        albums, artists = self.mass.music.albums, self.mass.music.artists
        phases = [
            _LinkPhase(
                "albums",
                albums,
                DB_TABLE_ALBUMS,
                _albums_to_identify_query(),
                {"stale": int(time() - REFRESH_INTERVAL)},
            ),
            _LinkPhase("artists", artists, DB_TABLE_ARTISTS, _artists_to_identify_query(), {}),
            _LinkPhase(
                "tracks", self.mass.music.tracks, DB_TABLE_TRACKS, _tracks_to_identify_query(), {}
            ),
        ]
        # a provider loaded after an item was looked up still lacks its (cached) links
        for domain, first_seen in linked_domains.items():
            params = {"domain": domain, "seen": first_seen}
            phases.append(
                _LinkPhase(
                    f"albums missing {domain}",
                    albums,
                    DB_TABLE_ALBUMS,
                    _relink_query(DB_TABLE_ALBUMS, MediaType.ALBUM, ExternalID.MB_ALBUM),
                    params,
                )
            )
            phases.append(
                _LinkPhase(
                    f"artists missing {domain}",
                    artists,
                    DB_TABLE_ARTISTS,
                    _relink_query(DB_TABLE_ARTISTS, MediaType.ARTIST, ExternalID.MB_ARTIST),
                    params,
                )
            )
        return phases

    def _refresh_musicbrainz_linked_domains(self) -> dict[str, int]:
        """
        Return since when each loaded music provider MusicBrainz links to has been linked.

        A provider loaded since the previous run starts now and one no longer loaded is
        dropped. The very first run seeds the providers present with 0, as the library was
        linked to those while it was identified.
        """
        # persisted as "domain:epoch" entries, the map itself not being a config value type
        entries = cast(
            "list[str] | None",
            self.mass.config.get_raw_core_config_value(
                self.domain, CONF_MUSICBRAINZ_LINKED_DOMAINS
            ),
        )
        stored: dict[str, int] = {}
        for entry in entries or []:
            domain, _, seen = entry.partition(":")
            if seen.isdigit():
                stored[domain] = int(seen)
        # the providers the link step maps to: those with a loaded instance, available or not
        present = [
            domain
            for domain in MUSICBRAINZ_LINK_DOMAINS
            if self.mass.music.get_provider_instances(domain, return_unavailable=True)
        ]
        first_seen = 0 if entries is None else int(time())
        # a provider whose instance was gone for a run comes back as a new one: the items
        # identified meanwhile lack its links, so its relink phase re-checks those still not
        # mapped to it, from the MusicBrainz cache
        linked = {domain: stored.get(domain, first_seen) for domain in sorted(present)}
        # an empty map is persisted too: it tells the first run apart from a later one
        if entries is None or linked != stored:
            self.mass.config.set_raw_core_config_value(
                self.domain,
                CONF_MUSICBRAINZ_LINKED_DOMAINS,
                [f"{domain}:{seen}" for domain, seen in linked.items()],
            )
        return linked

    async def _pending_musicbrainz_link_counts(self) -> dict[str, int]:
        """Return how many albums, artists and tracks still await their MusicBrainz lookup."""
        counts: dict[str, int] = {}
        stale = {"stale": int(time() - REFRESH_INTERVAL)}
        for name, table, query in (
            ("albums", DB_TABLE_ALBUMS, _albums_to_identify_query()),
            ("artists", DB_TABLE_ARTISTS, _artists_to_identify_query()),
            ("tracks", DB_TABLE_TRACKS, _tracks_to_identify_query()),
        ):
            counts[name] = await self.mass.music.database.get_count_from_query(
                f"SELECT 1 FROM {table} WHERE {_valid_metadata_guard(table)} AND {query}", stale
            )
        return counts

    def _metadata_rate_limited(self, feature: ProviderFeature) -> bool:
        """
        Return whether a metadata provider offering the given feature is rate limiting.

        :param feature: The metadata feature an item's refresh needs, e.g. ARTIST_METADATA.
        """
        return any(
            prov.rate_limited for prov in self.providers if feature in prov.supported_features
        )

    async def _get_scan_batch[ItemCls: MediaItemType](
        self,
        media_controller: MediaControllerBase[ItemCls],
        table: str,
        query: str,
        query_params: dict[str, Any] | None = None,
        *,
        limit: int = METADATA_SCAN_BATCH_SIZE,
        order_by: str = "random",
    ) -> list[ItemCls]:
        """Fetch a metadata-scan batch, tolerating rows with corrupt metadata JSON."""
        try:
            items = await media_controller.get_library_items_by_query(
                limit=limit,
                order_by=order_by,
                extra_query_parts=[query],
                extra_query_params=query_params,
            )
        except sqlite3.OperationalError as err:
            if "malformed JSON" not in str(err):
                raise
            await self._report_corrupt_metadata_rows(table)
            return await media_controller.get_library_items_by_query(
                limit=limit,
                order_by=order_by,
                extra_query_parts=[f"{_valid_metadata_guard(table)} AND {query}"],
                extra_query_params=query_params,
            )
        # a clean scan proves the table currently holds no corrupt rows
        self._corrupt_metadata_rows.pop(table, None)
        return items

    async def _report_corrupt_metadata_rows(self, table: str) -> None:
        """Report library rows whose metadata column holds invalid JSON."""
        rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id, name FROM {table} "
            f"WHERE {table}.metadata IS NOT NULL AND NOT json_valid({table}.metadata)",
            limit=25,
        )
        # keep the findings for the diagnostics report, replacing the previous
        # pass so repaired rows drop out again
        if rows:
            self._corrupt_metadata_rows[table] = [
                {"item_id": row["item_id"], "name": row["name"]} for row in rows
            ]
        else:
            self._corrupt_metadata_rows.pop(table, None)
        for row in rows:
            message = (
                f"'{row['name']}' has corrupt metadata and was skipped. To repair, remove "
                f"'{row['name']}' from the library; it will be re-added with fresh metadata "
                f"on the next library sync ({table} id {row['item_id']})."
            )
            report_current_task_failure(message)
            self.logger.warning(message)


def _duplicate_album_sibling_guard() -> str:
    """Return a query part that selects albums which may be a duplicate of another library row."""
    shares_artist = (
        f"EXISTS (SELECT 1 FROM {DB_TABLE_ALBUM_ARTISTS} own "
        f"JOIN {DB_TABLE_ALBUM_ARTISTS} other ON other.artist_id = own.artist_id "
        f"WHERE own.album_id = {DB_TABLE_ALBUMS}.item_id AND other.album_id = dup.item_id)"
    )
    # a title that normalizes to nothing (e.g. Ed Sheeran's '+', '=' and '÷') matches every
    # other such title, so those fall back to their raw spelling like the album comparison does
    same_title = (
        f"({DB_TABLE_ALBUMS}.search_name != '' OR "
        f"REPLACE({DB_TABLE_ALBUMS}.name,' ','') = REPLACE(dup.name,' ',''))"
    )
    # a provider that spells out the retail suffix stores the album under the plain name
    # plus that suffix, so the pair is related from either side. The raw title decides
    # which side spelled it out, so an ordinary title that merely ends in those letters
    # ("Step") is left alone.
    # Every alternative stays an equality on dup.search_name, keeping the name index in use.
    own_name = f"{DB_TABLE_ALBUMS}.search_name"
    matches_name = [f"dup.search_name = {own_name}"]
    for suffix in ALBUM_RETAIL_SUFFIX_KEYS:
        matches_name.append(
            f"({album_retail_suffix_sql_match('dup.name', suffix)} "
            f"AND dup.search_name = {own_name} || '{suffix}')"
        )
        matches_name.append(
            f"({album_retail_suffix_sql_match(f'{DB_TABLE_ALBUMS}.name', suffix)} "
            f"AND dup.search_name = "
            f"substr({own_name}, 1, length({own_name}) - {len(suffix)}))"
        )
    same_name = " OR ".join(matches_name)
    # deliberately an identity-only pre-filter: which editions may be merged is decided by
    # the album comparison, which escalates an ambiguous edition to tracklists and
    # MusicBrainz and rejects a recording-changing one (live, remix, ...) outright
    return (
        f"EXISTS (SELECT 1 FROM {DB_TABLE_ALBUMS} dup "
        f"WHERE dup.item_id != {DB_TABLE_ALBUMS}.item_id "
        f"AND ({same_name}) "
        f"AND {same_title} AND {shares_artist})"
    )


def _missing_metadata_query(table: str, *, description: bool = False) -> str:
    """
    Return a query part selecting never-refreshed items that lack images (or a description).

    :param table: The library table to select from.
    :param description: Whether a missing description also selects the item.
    """
    images = f"json_extract({table}.metadata,'$.images')"
    missing = [f"({images} ISNULL OR {images} = '[]')"]
    if description:
        missing.append(f"json_extract({table}.metadata,'$.description') ISNULL")
    never_refreshed = f"json_extract({table}.metadata,'$.last_refresh') ISNULL"
    return f"({' OR '.join(missing)}) AND {never_refreshed}"


def _musicbrainz_lookup_marker(table: str) -> str:
    """Return the SQL expression of when an item was last looked up on MusicBrainz."""
    return f"json_extract({table}.metadata,'$.last_musicbrainz_lookup')"


def _has_musicbrainz_id(item_id: str, media_type: MediaType, id_type: ExternalID) -> str:
    """
    Return a query part that is true for an item carrying the given MusicBrainz id.

    :param item_id: SQL expression of the library item id, e.g. ``albums.item_id``.
    :param media_type: Media type of the item.
    :param id_type: The MusicBrainz id type the item has to carry.
    """
    return (
        f"EXISTS (SELECT 1 FROM {DB_TABLE_EXTERNAL_ID_LOOKUP} "
        f"WHERE {DB_TABLE_EXTERNAL_ID_LOOKUP}.media_type = '{media_type.value}' "
        f"AND {DB_TABLE_EXTERNAL_ID_LOOKUP}.external_id_type = '{id_type.value}' "
        f"AND {DB_TABLE_EXTERNAL_ID_LOOKUP}.item_id = {item_id})"
    )


def _albums_to_identify_query() -> str:
    """Return a query part selecting albums due for a MusicBrainz lookup (param ``stale``)."""
    marker = _musicbrainz_lookup_marker(DB_TABLE_ALBUMS)
    identified = _has_musicbrainz_id(
        f"{DB_TABLE_ALBUMS}.item_id", MediaType.ALBUM, ExternalID.MB_ALBUM
    )
    # an album MusicBrainz did not know is looked up again once its lookup is stale
    return f"({marker} ISNULL OR ({marker} < :stale AND NOT {identified}))"


def _artists_to_identify_query() -> str:
    """Return a query part selecting artists due for a MusicBrainz lookup."""
    marker = _musicbrainz_lookup_marker(DB_TABLE_ARTISTS)
    identified = _has_musicbrainz_id(
        f"{DB_TABLE_ARTISTS}.item_id", MediaType.ARTIST, ExternalID.MB_ARTIST
    )
    # the credits of an identified album name the artist at the cost of a cached lookup
    album_identified = _has_musicbrainz_id(
        f"{DB_TABLE_ALBUM_ARTISTS}.album_id", MediaType.ALBUM, ExternalID.MB_ALBUM
    )
    has_identified_album = (
        f"EXISTS (SELECT 1 FROM {DB_TABLE_ALBUM_ARTISTS} "
        f"WHERE {DB_TABLE_ALBUM_ARTISTS}.artist_id = {DB_TABLE_ARTISTS}.item_id "
        f"AND {album_identified})"
    )
    return f"({marker} ISNULL AND NOT {identified} AND {has_identified_album})"


def _tracks_to_identify_query() -> str:
    """Return a query part selecting tracks due for a MusicBrainz lookup."""
    marker = _musicbrainz_lookup_marker(DB_TABLE_TRACKS)
    identified = _has_musicbrainz_id(
        f"{DB_TABLE_TRACKS}.item_id", MediaType.TRACK, ExternalID.MB_RECORDING
    )
    # the tracklist of an identified album names the track at the cost of a cached lookup
    album_identified = _has_musicbrainz_id(
        f"{DB_TABLE_ALBUM_TRACKS}.album_id", MediaType.ALBUM, ExternalID.MB_ALBUM
    )
    has_identified_album = (
        f"EXISTS (SELECT 1 FROM {DB_TABLE_ALBUM_TRACKS} "
        f"WHERE {DB_TABLE_ALBUM_TRACKS}.track_id = {DB_TABLE_TRACKS}.item_id "
        f"AND {album_identified})"
    )
    return f"({marker} ISNULL AND NOT {identified} AND {has_identified_album})"


def _relink_query(table: str, media_type: MediaType, id_type: ExternalID) -> str:
    """
    Return a query part selecting identified items that miss the links of a music provider.

    Selects items not mapped to the provider (param ``domain``) that were looked up before
    it was loaded (param ``seen``) or never looked up at all.

    :param table: The library table to select from.
    :param media_type: Media type of the items.
    :param id_type: The MusicBrainz id type an identified item carries.
    """
    identified = _has_musicbrainz_id(f"{table}.item_id", media_type, id_type)
    mapped = (
        f"EXISTS (SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} "
        f"WHERE {DB_TABLE_PROVIDER_MAPPINGS}.media_type = '{media_type.value}' "
        f"AND {DB_TABLE_PROVIDER_MAPPINGS}.item_id = {table}.item_id "
        f"AND {DB_TABLE_PROVIDER_MAPPINGS}.provider_domain = :domain)"
    )
    marker = _musicbrainz_lookup_marker(table)
    # an id taken from tags or a local server leaves the marker unset: never linked at all
    return f"({identified} AND NOT {mapped} AND ({marker} ISNULL OR {marker} <= :seen))"


def _valid_metadata_guard(table: str) -> str:
    """Return a query part that excludes rows with invalid JSON in the metadata column."""
    # sqlite's json functions raise a fatal 'malformed JSON' error on invalid input,
    # which would fail the entire scan query because of a single corrupt row
    return f"({table}.metadata IS NULL OR json_valid({table}.metadata))"
