"""All logic for metadata retrieval."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
import threading
from collections import OrderedDict
from time import time
from typing import TYPE_CHECKING, cast
from uuid import NAMESPACE_URL, uuid5

from music_assistant_models.auth import Scope
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature, ProviderType
from music_assistant_models.media_items import BrowseFolder

from music_assistant.constants import (
    CONF_LANGUAGE,
    DB_TABLE_ARTISTS,
    DB_TABLE_PLAYLISTS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.images import cleanup_thumb_cache
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.helpers.util import try_parse_int
from music_assistant.models.core_controller import CoreController
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_ENABLE_ONLINE_METADATA,
    CONF_ENABLE_RADIO_METADATA_LOOKUP,
    CONF_PREFER_LOCAL_GENRES,
    CONF_THUMB_CACHE_MAX_SIZE,
    DEFAULT_LANGUAGE,
    DEFAULT_THUMB_CACHE_MAX_SIZE_MB,
    LOCALES,
    METADATA_LOOKUP_TASK_ID_PREFIX,
    METADATA_SCAN_BATCH_SIZE,
    MISSING_ARTIST_METADATA_SCAN_TASK_ID,
    PLAYLIST_METADATA_SCAN_TASK_ID,
    REFRESH_INTERVAL,
    THUMB_CACHE_CLEANUP_TASK_ID,
)
from .enrichment import MetadataEnrichmentMixin
from .images import ImageProxyMixin
from .radio import RadioArtworkMixin

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, CoreConfig
    from music_assistant_models.media_items import (
        Album,
        Artist,
        Audiobook,
        MediaItemType,
        Playlist,
        Podcast,
        Track,
    )

    from music_assistant import MusicAssistant
    from music_assistant.controllers.music.media.base import MediaControllerBase
    from music_assistant.helpers.json import SerializableType
    from music_assistant.models.metadata_provider import MetadataProvider


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

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        return (
            ConfigEntry(
                key=CONF_LANGUAGE,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=DEFAULT_LANGUAGE,
                options=[ConfigValueOption(key, title=value) for key, value in LOCALES.items()],
            ),
            ConfigEntry(
                key=CONF_ENABLE_ONLINE_METADATA,
                type=ConfigEntryType.BOOLEAN,
                required=False,
                default_value=True,
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
        # make sure that our directory with collage images exists
        self._collage_images_dir = os.path.join(self.mass.cache_path, "collage_images")
        if not await asyncio.to_thread(os.path.exists, self._collage_images_dir):
            await asyncio.to_thread(os.mkdir, self._collage_images_dir)

    async def post_setup(self) -> None:
        """Handle logic after all core controllers have been set up."""
        # canonical opaque-id endpoint, served by both the public webserver
        # and the streams server (the latter is what player metadata URLs hit)
        self.mass.streams.register_dynamic_route("/imageproxy/*", self.handle_imageproxy)
        self.mass.webserver.register_dynamic_route("/imageproxy/*", self.handle_imageproxy)
        self._register_maintenance_tasks()

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
            if item.media_type == MediaType.ARTIST:
                await self._update_artist_metadata(
                    cast("Artist", item), force_refresh=force_refresh
                )
            if item.media_type == MediaType.ALBUM:
                await self._update_album_metadata(cast("Album", item), force_refresh=force_refresh)
            if item.media_type == MediaType.TRACK:
                await self._update_track_metadata(cast("Track", item), force_refresh=force_refresh)
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

    @api_command("metadata/get_track_lyrics", required_scope=Scope.LIBRARY_READ)
    async def get_track_lyrics(
        self,
        track: Track,
    ) -> tuple[str | None, str | None]:
        """
        Get lyrics for given track from metadata providers.

        Returns a tuple of (lyrics, lrc_lyrics) if found.
        """
        if track.metadata and track.metadata.lyrics:
            return track.metadata.lyrics, track.metadata.lrc_lyrics

        if track.provider == "library":
            # try to update metadata first
            await self._update_track_metadata(track, force_refresh=False)
            return track.metadata.lyrics, track.metadata.lrc_lyrics

        # prefer lyrics from the track's own provider
        track_provider = self.mass.get_provider(track.provider, provider_type=MusicProvider)
        if track_provider and ProviderFeature.LYRICS in track_provider.supported_features:
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

    async def get_diagnostics(self) -> dict[str, SerializableType] | None:
        """Return diagnostics info for this controller to include in diagnostics reports."""
        if not self._corrupt_metadata_rows:
            return None
        return {"corrupt_metadata_rows": cast("SerializableType", self._corrupt_metadata_rows)}

    def _register_maintenance_tasks(self) -> None:
        """Register the recurring metadata maintenance background tasks."""
        # Spread across the full day so instances don't all hit the shared MusicBrainz mirror at once
        utc_hour, utc_minute = divmod(random.randint(0, 24 * 60 - 1), 60)
        desired_schedule = TaskSchedule.daily(hour=utc_hour, minute=utc_minute)
        self.mass.tasks.register_scheduled_task(
            task_id=MISSING_ARTIST_METADATA_SCAN_TASK_ID,
            name="Scan missing artist metadata",
            handler=self._scan_missing_artist_metadata,
            schedule=desired_schedule,
            translation_key="scan_missing_artist_metadata",
            translation_owner=self.translation_owner,
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

    @staticmethod
    def _get_metadata_lookup_task_id(uri: str) -> str:
        """Return deterministic task id for a metadata lookup."""
        return f"{METADATA_LOOKUP_TASK_ID_PREFIX}_{uuid5(NAMESPACE_URL, uri).hex}"

    async def _scan_missing_artist_metadata(self) -> None:
        """Scan for artists with missing metadata."""
        update_current_task_progress_text("Searching for artists with missing metadata")
        missing_images = (
            f"(json_extract({DB_TABLE_ARTISTS}.metadata,'$.images') ISNULL "
            f"OR json_extract({DB_TABLE_ARTISTS}.metadata,'$.images') = '[]')"
        )
        missing_description = f"json_extract({DB_TABLE_ARTISTS}.metadata,'$.description') ISNULL"
        never_refreshed = f"json_extract({DB_TABLE_ARTISTS}.metadata,'$.last_refresh') ISNULL"
        query = f"({missing_images} OR {missing_description}) AND {never_refreshed}"
        artists = await self._get_scan_batch(self.mass.music.artists, DB_TABLE_ARTISTS, query)
        if not artists:
            update_current_task_progress_text("No artists with missing metadata found")
            return
        for index, artist in enumerate(artists, 1):
            try:
                update_current_task_progress_from_index(
                    index,
                    len(artists),
                    f"Refreshing metadata for artist {index}/{len(artists)}: {artist.name}",
                )
                await self._update_artist_metadata(artist, force_refresh=False)
            except Exception as err:
                report_current_task_failure(f"{artist.name}: {err}")
                self.logger.warning(
                    "Error while updating artist metadata for %s: %s",
                    artist.name,
                    str(err),
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
        update_current_task_progress(100, f"Processed {len(artists)} artist(s)")

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

    async def _get_scan_batch[ItemCls: MediaItemType](
        self,
        media_controller: MediaControllerBase[ItemCls],
        table: str,
        query: str,
    ) -> list[ItemCls]:
        """Fetch a metadata-scan batch, tolerating rows with corrupt metadata JSON."""
        try:
            items = await media_controller.get_library_items_by_query(
                limit=METADATA_SCAN_BATCH_SIZE,
                order_by="random",
                extra_query_parts=[query],
            )
        except sqlite3.OperationalError as err:
            if "malformed JSON" not in str(err):
                raise
            await self._report_corrupt_metadata_rows(table)
            return await media_controller.get_library_items_by_query(
                limit=METADATA_SCAN_BATCH_SIZE,
                order_by="random",
                extra_query_parts=[f"{_valid_metadata_guard(table)} AND {query}"],
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


def _valid_metadata_guard(table: str) -> str:
    """Return a query part that excludes rows with invalid JSON in the metadata column."""
    # sqlite's json functions raise a fatal 'malformed JSON' error on invalid input,
    # which would fail the entire scan query because of a single corrupt row
    return f"({table}.metadata IS NULL OR json_valid({table}.metadata))"
