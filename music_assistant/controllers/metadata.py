"""All logic for metadata retrieval."""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import random
import threading
from base64 import b64encode
from collections import OrderedDict
from contextlib import suppress
from dataclasses import replace
from time import time
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import aiofiles
from aiohttp import web
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.helpers import get_global_cache_value
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Playlist,
    Podcast,
    Track,
)
from music_assistant_models.streamdetails import StreamMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    CONF_LANGUAGE,
    DB_TABLE_ARTISTS,
    DB_TABLE_PLAYLISTS,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress,
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.images import (
    cleanup_thumb_cache,
    create_collage,
    create_thumb_hash,
    detect_image_content_format,
    get_image_data,
    get_image_thumb,
)
from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.tags import split_artists
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.helpers.util import parse_title_and_version, try_parse_int
from music_assistant.models.core_controller import CoreController
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.config_entries import CoreConfig
    from music_assistant_models.streamdetails import StreamDetails

    from music_assistant import MusicAssistant
    from music_assistant.models.metadata_provider import MetadataProvider
    from music_assistant.providers.musicbrainz import MusicbrainzProvider, MusicBrainzReleaseGroup


def _detect_image_format(path: str) -> str:
    """Detect image format from file path extension, defaulting to jpg."""
    # strip any query suffix (e.g. a cache-busting ?cs=) before extension detection
    match pathlib.PurePath(path.split("?", 1)[0]).suffix.lower():
        case ".svg":
            return "svg"
        case ".png":
            return "png"
        case _:
            return "jpg"


# Map of normalised imageproxy `fmt` values to standards-compliant MIME types.
# Used both to validate the client-supplied `fmt` query parameter and to set
# the Content-Type on the response.
_IMAGEPROXY_CONTENT_TYPES: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "svg": "image/svg+xml",
}


def _normalize_imageproxy_format(value: str | None) -> str | None:
    """Return a validated, lowercase imageproxy format, or None when invalid."""
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in _IMAGEPROXY_CONTENT_TYPES:
        return normalized
    return None


LOCALES = {
    "af_ZA": "African",
    "ar_AE": "Arabic (United Arab Emirates)",
    "ar_EG": "Arabic (Egypt)",
    "ar_SA": "Saudi Arabia",
    "bg_BG": "Bulgarian",
    "cs_CZ": "Czech",
    "zh_CN": "Chinese",
    "hr_HR": "Croatian",
    "da_DK": "Danish",
    "de_DE": "German",
    "el_GR": "Greek",
    "en_AU": "English (AU)",
    "en_US": "English (US)",
    "en_GB": "English (UK)",
    "es_ES": "Spanish",
    "et_EE": "Estonian",
    "fi_FI": "Finnish",
    "fr_FR": "French",
    "hu_HU": "Hungarian",
    "is_IS": "Icelandic",
    "it_IT": "Italian",
    "lt_LT": "Lithuanian",
    "lv_LV": "Latvian",
    "ja_JP": "Japanese",
    "ko_KR": "Korean",
    "nl_NL": "Dutch",
    "nb_NO": "Norwegian Bokmål",
    "pl_PL": "Polish",
    "pt_PT": "Portuguese",
    "ro_RO": "Romanian",
    "ru_RU": "Russian",
    "sk_SK": "Slovak",
    "sl_SI": "Slovenian",
    "sr_RS": "Serbian",
    "sv_SE": "Swedish",
    "tr_TR": "Turkish",
    "uk_UA": "Ukrainian",
}

DEFAULT_LANGUAGE = "en_US"

# Radio stream artwork cache settings
CACHE_CATEGORY_RADIO_ARTWORK = 101
CACHE_EXPIRATION_RADIO_ARTWORK = 86400 * 90  # 90 days
CACHE_EXPIRATION_RADIO_ARTWORK_MISS = 86400 * 7  # 7 days
AD_DETECTION_PHRASES = ("asset link", "asset stop", "asset spot", "advert", "promo")

REFRESH_INTERVAL = 60 * 60 * 24 * 90  # 90 days
CONF_ENABLE_ONLINE_METADATA = "enable_online_metadata"
CONF_PREFER_LOCAL_GENRES = "prefer_local_genres"
CONF_ENABLE_RADIO_METADATA_LOOKUP = "enable_radio_metadata_lookup"
MISSING_ARTIST_METADATA_SCAN_TASK_ID = "metadata_missing_artist_metadata_scan_v2"
PLAYLIST_METADATA_SCAN_TASK_ID = "metadata_playlist_metadata_scan_v2"
THUMB_CACHE_CLEANUP_TASK_ID = "metadata_thumb_cache_cleanup_v2"
METADATA_LOOKUP_TASK_ID_PREFIX = "metadata_lookup"
METADATA_SCAN_BATCH_SIZE = 5
CONF_THUMB_CACHE_MAX_SIZE = "thumb_cache_max_size"
DEFAULT_THUMB_CACHE_MAX_SIZE_MB = 500

# Image-id system: maps a sha256(provider+path) hash to the (provider, path) tuple
# so the imageproxy can be addressed by an opaque short id instead of a long
# query string carrying the raw (often URL-shaped) path. The high category
# number matches the convention used elsewhere in this controller and avoids
# collisions with providers that use low category integers under the default
# cache namespace.
CACHE_CATEGORY_IMAGE_IDS = 102
_IMAGE_ID_LRU_MAX = 10000
_IMAGE_ID_CACHE_TTL = 86400 * 365  # 1 year, refreshed on each write

# Sizes accepted by the imageproxy. 0 means "no resize". The set is small enough
# to bound PIL memory + thumbnail cache cardinality; expand if a real use case appears.
_ALLOWED_IMAGEPROXY_SIZES = frozenset({0, 80, 160, 256, 512, 1024})

_IMAGEPROXY_PATH_PREFIX = "/imageproxy/"


class MetaDataController(CoreController):
    """Several helpers to search and store metadata for mediaitems."""

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
        # image-id LRU: image_id -> (provider, path). Acts as a write-through
        # hot cache in front of the cache controller so that resolving an image
        # by id never blocks on sqlite if the URL was generated recently.
        # The lock is needed because compute_image_id() runs from the executor
        # thread during outbound websocket serialization.
        self._image_id_lru: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._image_id_lock = threading.Lock()

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
                label="Preferred language",
                required=False,
                default_value=DEFAULT_LANGUAGE,
                description="Preferred language for metadata.\n\n"
                "Note that English will always be used as fallback when content "
                "in your preferred language is not available.",
                options=[ConfigValueOption(value, key) for key, value in LOCALES.items()],
            ),
            ConfigEntry(
                key=CONF_ENABLE_ONLINE_METADATA,
                type=ConfigEntryType.BOOLEAN,
                label="Enable metadata retrieval from online metadata providers",
                required=False,
                default_value=True,
                description="Enable online metadata lookups.\n\n"
                "This will allow Music Assistant to fetch additional metadata from (enabled) "
                "metadata providers, such as The Audio DB and Fanart.tv.\n\n"
                "Note that these online sources are only queried when no information is already "
                "available from local files or the music providers and local artwork/metadata "
                "will always have preference over online sources so consider metadata from online "
                "sources as complementary only.\n\n"
                "The retrieval of additional rich metadata is a process that is executed slowly "
                "in the background to not overload these free services with requests. "
                "You can speedup the process by storing the images and other metadata locally.",
            ),
            ConfigEntry(
                key=CONF_PREFER_LOCAL_GENRES,
                type=ConfigEntryType.BOOLEAN,
                label="Use local genre metadata only when available",
                required=False,
                default_value=False,
                description="When enabled, online metadata providers will not add genres to "
                "items that already have a genre from a local source such as a file tag "
                "or NFO file. Items with no local genre still receive genres from online "
                "providers as usual.",
            ),
            ConfigEntry(
                key=CONF_ENABLE_RADIO_METADATA_LOOKUP,
                type=ConfigEntryType.BOOLEAN,
                label="Enable artist/track artwork lookup for radio streams",
                required=False,
                default_value=True,
                description="Look up artist and track artwork for radio streams "
                "from online sources when the station provides Artist - Track metadata.\n\n"
                "When disabled, radio streams show only the station logo (when available).",
            ),
            ConfigEntry(
                key=CONF_THUMB_CACHE_MAX_SIZE,
                type=ConfigEntryType.INTEGER,
                label="Maximum thumbnail cache size (MB)",
                required=False,
                default_value=DEFAULT_THUMB_CACHE_MAX_SIZE_MB,
                range=(50, 5000),
                description="Maximum total size in megabytes for the on-disk thumbnail cache.\n\n"
                "Oldest thumbnails are automatically removed when this limit is exceeded.",
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

    @api_command("metadata/set_default_preferred_language")
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

    @api_command("metadata/set_preferred_language")
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

    @api_command("metadata/update_metadata")
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
            translation_key="background_task.update_metadata",
            translation_args=[item.name],
            metadata={
                "task_domain": "metadata_lookup",
                "item_uri": item.uri,
            },
        )

    def compute_image_id(self, provider: str, path: str) -> str:
        """
        Return the opaque imageproxy image id for the given image.

        The id is deterministic: the same (provider, path) pair always
        yields the same id, across processes and restarts. Calling this
        also ensures the id is resolvable back to (provider, path) by
        a subsequent imageproxy request.

        Safe to call from any thread.

        :param provider: Provider id that owns / can resolve the image.
        :param path: Image path or URL as the provider knows it.
        """
        image_id = create_thumb_hash(provider, path)
        with self._image_id_lock:
            if image_id in self._image_id_lru:
                self._image_id_lru.move_to_end(image_id)
                return image_id
            self._image_id_lru[image_id] = (provider, path)
            while len(self._image_id_lru) > _IMAGE_ID_LRU_MAX:
                self._image_id_lru.popitem(last=False)
        # the to_dict hook calls us from the executor when running under
        # _send_message; only call create_task directly when we know we are
        # on the loop thread, otherwise hop across via call_soon_threadsafe
        coro = self._persist_image_id(image_id, provider, path)
        if threading.get_ident() == self.mass.loop_thread_id:
            self.mass.create_task(coro)
        else:
            self.mass.loop.call_soon_threadsafe(self.mass.create_task, coro)
        return image_id

    async def resolve_image_id(self, image_id: str) -> tuple[str, str] | None:
        """
        Return the (provider, path) tuple for a previously registered image id.

        :param image_id: The opaque id as produced by `compute_image_id`.
        """
        with self._image_id_lock:
            if cached := self._image_id_lru.get(image_id):
                self._image_id_lru.move_to_end(image_id)
                return cached
        cached_db = await self.cache.get(
            key=image_id,
            category=CACHE_CATEGORY_IMAGE_IDS,
            provider=self.domain,
        )
        if isinstance(cached_db, dict):
            provider = cached_db.get("provider")
            path = cached_db.get("path")
            if isinstance(provider, str) and isinstance(path, str):
                result = (provider, path)
                with self._image_id_lock:
                    self._image_id_lru[image_id] = result
                    while len(self._image_id_lru) > _IMAGE_ID_LRU_MAX:
                        self._image_id_lru.popitem(last=False)
                return result
        return None

    async def get_image_data_for_item(
        self,
        media_item: MediaItemType,
        img_type: ImageType = ImageType.THUMB,
        size: int = 0,
    ) -> bytes | None:
        """Get image data for given MedaItem."""
        img_path = await self.get_image_url_for_item(
            media_item=media_item,
            img_type=img_type,
        )
        if not img_path:
            return None
        thumbnail = await self.get_thumbnail(img_path, provider="builtin", size=size)

        return cast("bytes", thumbnail)

    async def get_image_url_for_item(
        self,
        media_item: MediaItemType | ItemMapping,
        img_type: ImageType = ImageType.THUMB,
        resolve: bool = True,
    ) -> str | None:
        """Get url to image for given media media_item."""
        if not media_item:
            return None

        if isinstance(media_item, ItemMapping):
            # Check if the ItemMapping already has an image - avoid expensive API call
            if media_item.image and media_item.image.type == img_type:
                if media_item.image.remotely_accessible and resolve:
                    return self.get_image_url(media_item.image)
                if not media_item.image.remotely_accessible:
                    return media_item.image.path

            # Only retrieve full item if we don't have the image we need
            if not media_item.uri:
                return None
            retrieved_item = await self.mass.music.get_item_by_uri(media_item.uri)
            if isinstance(retrieved_item, BrowseFolder):
                return None  # can not happen, but guard for type checker
            media_item = retrieved_item

        if media_item and media_item.metadata.images:
            for img in media_item.metadata.images:
                if img.type != img_type:
                    continue
                if not img.remotely_accessible and not resolve:
                    # ignore image if its not remotely accessible and we don't allow resolving
                    continue
                return self.get_image_url(img, prefer_proxy=not img.remotely_accessible)

        # retry with track's album
        if isinstance(media_item, Track) and media_item.album:
            return await self.get_image_url_for_item(media_item.album, img_type, resolve)

        # try artist instead for albums
        if isinstance(media_item, Album) and media_item.artists:
            return await self.get_image_url_for_item(media_item.artists[0], img_type, resolve)

        # last resort: track artist(s)
        if isinstance(media_item, Track) and media_item.artists:
            for artist in media_item.artists:
                return await self.get_image_url_for_item(artist, img_type, resolve)

        return None

    def get_image_url(
        self,
        image: MediaItemImage,
        size: int = 0,
        prefer_proxy: bool = False,
        image_format: str | None = None,
        prefer_stream_server: bool = False,
    ) -> str:
        """Get (proxied) URL for MediaItemImage."""
        if image_format is None:
            image_format = _detect_image_format(image.path)
        if image_format == "svg":
            # SVGs don't need resizing
            size = 0
        if not image.remotely_accessible or prefer_proxy or size:
            # short opaque id form; same id as the thumbnail cache key
            image_id = self.compute_image_id(image.provider, image.path)
            base_url = (
                self.mass.streams.base_url if prefer_stream_server else self.mass.webserver.base_url
            )
            return f"{base_url}/imageproxy/{image_id}?size={size}&fmt={image_format}"
        return image.path

    async def get_thumbnail(
        self,
        path: str,
        provider: str,
        size: int | None = None,
        base64: bool = False,
        image_format: str | None = None,
        flatten_transparency: bool = False,
    ) -> bytes | str:
        """Get/create thumbnail image for path (image url or local path)."""
        if image_format is None:
            image_format = _detect_image_format(path)
        thumbnail_bytes, content_format = await self._resolve_thumbnail(
            path, provider, size, image_format, flatten_transparency
        )
        if base64:
            enc_image = b64encode(thumbnail_bytes).decode()
            return f"data:{_IMAGEPROXY_CONTENT_TYPES[content_format]};base64,{enc_image}"
        return thumbnail_bytes

    async def handle_imageproxy(self, request: web.Request) -> web.Response:
        """
        Serve an image for a `/imageproxy/<image_id>?size=&fmt=` request.

        This is the canonical imageproxy endpoint: clients build the URL by
        taking the `proxy_id` from a `MediaItemImage` and appending it as a
        single path segment, optionally with `size` and `fmt` query parameters.
        """
        # require exactly /imageproxy/<id> (optionally with a trailing slash);
        # extra path segments such as /imageproxy/foo/<id> must not validate
        if not request.path.startswith(_IMAGEPROXY_PATH_PREFIX):
            return web.Response(status=400)
        image_id = request.path[len(_IMAGEPROXY_PATH_PREFIX) :].rstrip("/").lower()
        if len(image_id) != 64 or any(c not in "0123456789abcdef" for c in image_id):
            return web.Response(status=400)
        try:
            size = int(request.query.get("size", "0"))
        except ValueError:
            return web.Response(status=400)
        if size not in _ALLOWED_IMAGEPROXY_SIZES:
            return web.Response(status=400)
        resolved = await self.resolve_image_id(image_id)
        if resolved is None:
            return web.Response(status=404)
        provider, path = resolved
        image_format = _normalize_imageproxy_format(
            request.query.get("fmt")
        ) or _detect_image_format(path)
        return await self._serve_thumbnail(path, provider, size, image_format)

    async def _resolve_thumbnail(
        self,
        path: str,
        provider: str,
        size: int | None,
        image_format: str,
        flatten_transparency: bool,
    ) -> tuple[bytes, str]:
        """
        Fetch image bytes and return them with their resolved content format.

        The served format can differ from the requested one (SVG is passed
        through unchanged and transparent sources may be kept as PNG), so the
        actual format is determined once here and reused by every caller.

        :param path: Image url or local path.
        :param provider: Provider identifier for the image source.
        :param size: Target thumbnail size (square), or None for original.
        :param image_format: Requested output format (jpg/jpeg/png/svg).
        :param flatten_transparency: Composite alpha onto white and keep JPEG when True.
        """
        if not self.mass.get_provider(provider) and not path.startswith("http"):
            raise ProviderUnavailableError
        if provider == "builtin" and path.startswith("/collage/"):
            # special case for collage images
            collage_rel = path.rsplit("/collage/", maxsplit=1)[-1]
            if not is_safe_path(collage_rel):
                raise FileNotFoundError("Invalid collage path")
            path = os.path.join(self._collage_images_dir, collage_rel)
        if image_format == "svg":
            return await get_image_data(self.mass, path, provider), "svg"
        thumbnail_bytes = await get_image_thumb(
            self.mass,
            path,
            size=size,
            provider=provider,
            image_format=image_format,
            flatten_transparency=flatten_transparency,
        )
        return thumbnail_bytes, detect_image_content_format(thumbnail_bytes) or image_format

    async def _serve_thumbnail(
        self, path: str, provider: str, size: int, image_format: str
    ) -> web.Response:
        """Fetch (or render+cache) the thumbnail and produce an HTTP response."""
        # `fmt=jpeg` is the explicit player-media request: players are sent a
        # JPEG for maximum compatibility, and since JPEG has no alpha channel we
        # composite transparency onto white. The auto-detected `fmt=jpg`/`png`
        # default (app/UI) instead keeps transparency as PNG.
        flatten_transparency = image_format == "jpeg"
        try:
            image_data, content_format = await self._resolve_thumbnail(
                path, provider, size, image_format, flatten_transparency
            )
        except Exception as err:
            # broadly catch all exceptions here to ensure we dont crash the request handler
            if isinstance(err, FileNotFoundError):
                self.logger.log(VERBOSE_LOG_LEVEL, "Image not found: %s", path)
            else:
                self.logger.warning(
                    "Error while fetching image %s: %s",
                    path,
                    str(err),
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )
            return web.Response(status=404)
        response_headers = {
            "Cache-Control": "max-age=31536000",
            "Access-Control-Allow-Origin": "*",
        }
        if content_format == "svg":
            # Sniffed SVGs from attacker-influenceable sources (radio favicons) are
            # served same-origin; without a CSP an embedded <script> would run.
            response_headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; sandbox"
            )
            response_headers["X-Content-Type-Options"] = "nosniff"
        return web.Response(
            body=image_data,
            headers=response_headers,
            content_type=_IMAGEPROXY_CONTENT_TYPES[content_format],
        )

    async def create_collage_image(
        self,
        images: list[MediaItemImage],
        filename: str,
        fanart: bool = False,
    ) -> MediaItemImage | None:
        """Create collage thumb/fanart image for (in-library) playlist."""
        if (len(images) < 8 and fanart) or len(images) < 3:
            # require at least some images otherwise this does not make a lot of sense
            return None
        # limit to 50 images to prevent we're going OOM
        if len(images) > 50:
            images = random.sample(images, 50)
        else:
            random.shuffle(images)
        try:
            # create collage thumb from playlist tracks
            # if playlist has no default image (e.g. a local playlist)
            dimensions = (2500, 1750) if fanart else (1500, 1500)
            img_data = await create_collage(self.mass, images, dimensions)
            # always overwrite existing path
            file_path = os.path.join(self._collage_images_dir, filename)
            async with aiofiles.open(file_path, "wb") as _file:
                await _file.write(img_data)
            del img_data
            return MediaItemImage(
                type=ImageType.FANART if fanart else ImageType.THUMB,
                path=f"/collage/{filename}",
                provider="builtin",
                remotely_accessible=False,
            )
        except Exception as err:
            self.logger.warning(
                "Error while creating playlist image: %s",
                str(err),
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )
        return None

    @api_command("metadata/get_track_lyrics")
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
            if (metadata := await provider.get_track_metadata(track)) and (
                metadata.lyrics or metadata.lrc_lyrics
            ):
                return metadata.lyrics, metadata.lrc_lyrics
        return None, None

    # ========== Radio Stream Artwork Methods ==========

    async def _get_release_group_artwork(
        self, mb_release_group: MusicBrainzReleaseGroup
    ) -> tuple[MediaItemMetadata, str] | None:
        """
        Try to get thumb artwork for a release group from metadata providers.

        :param mb_release_group: MusicBrainz release group to look up.
        :returns: Tuple of (metadata, provider_name) or None if not found.
        """
        self.logger.debug(
            "Looking up artwork for release group '%s' (mbid: %s)",
            mb_release_group.title,
            mb_release_group.id,
        )
        # Create a minimal Album object to pass the MusicBrainz release group ID
        # to metadata providers for artwork lookup.
        temp_album = Album(
            item_id="temp",
            provider="temp",
            name=mb_release_group.title,
            provider_mappings=set(),
        )
        temp_album.add_external_id(ExternalID.MB_RELEASEGROUP, mb_release_group.id)
        if mb_release_group.barcode:
            temp_album.add_external_id(ExternalID.BARCODE, mb_release_group.barcode)
        for provider in self.providers:
            if ProviderFeature.ALBUM_METADATA not in provider.supported_features:
                continue
            try:
                if metadata := await provider.get_album_metadata(temp_album):
                    if thumb := self._get_thumb_image(metadata):
                        return thumb, provider.name
            except (
                ProviderUnavailableError,
                ResourceTemporarilyUnavailable,
                InvalidDataError,
            ):
                pass
        return None

    async def _search_musicbrainz_with_variants(
        self,
        musicbrainz: MusicbrainzProvider,
        artist_name: str,
        track_name: str,
    ) -> tuple[Any, bool]:
        """
        Search MusicBrainz with fallback variants (swapped, without 'The').

        :param musicbrainz: MusicBrainz provider instance.
        :param artist_name: Artist name to search for.
        :param track_name: Track name to search for.
        :returns: Tuple of (mb_result, swapped) where swapped indicates artist/track were reversed.
        """
        # Try original order
        mb_result = await musicbrainz.get_release_group_by_track_name(artist_name, track_name)
        if mb_result:
            return mb_result, False

        # Try swapped (some stations send "Track - Artist")
        self.logger.debug(
            "No MusicBrainz match for '%s - %s', trying swapped",
            artist_name,
            track_name,
        )
        mb_result = await musicbrainz.get_release_group_by_track_name(track_name, artist_name)
        if mb_result:
            return mb_result, True

        # Try without "The " prefix
        artist_no_the = artist_name[4:] if artist_name.lower().startswith("the ") else None
        track_no_the = track_name[4:] if track_name.lower().startswith("the ") else None

        if artist_no_the:
            self.logger.debug(
                "No match, trying without 'The': '%s - %s'", artist_no_the, track_name
            )
            mb_result = await musicbrainz.get_release_group_by_track_name(artist_no_the, track_name)
            if mb_result:
                return mb_result, False

        if track_no_the:
            self.logger.debug(
                "No match, trying swapped without 'The': '%s - %s'", track_no_the, artist_name
            )
            mb_result = await musicbrainz.get_release_group_by_track_name(track_no_the, artist_name)
            if mb_result:
                return mb_result, True

        return None, False

    async def get_track_metadata_by_name(
        self,
        artist_name: str,
        track_name: str,
    ) -> tuple[MediaItemMetadata | None, str | None, str | None, str | None]:
        """
        Search for track/artist metadata by name.

        Checks library first for immediate results, then falls back to
        MusicBrainz for external metadata lookups.

        :param artist_name: Artist name to search for.
        :param track_name: Track title to search for.
        :returns: Tuple of (metadata, source_description, corrected_artist, corrected_track).
        """
        # Clean track name by stripping version suffixes and featuring credits
        clean_track_name, _ = parse_title_and_version(track_name, strip_for_search=True)

        # Check library track first - fast, no API calls, respects user-curated images
        if metadata := await self._get_library_track_metadata(artist_name, clean_track_name):
            return metadata, "library track", artist_name, clean_track_name

        # Use MusicBrainz to get IDs for accurate external metadata lookups
        musicbrainz_provider = self.mass.get_provider("musicbrainz")
        if not musicbrainz_provider:
            # No MusicBrainz, try library artist as fallback
            if metadata := await self._get_library_artist_metadata(artist_name):
                return metadata, f"library artist '{artist_name}'", artist_name, clean_track_name
            return None, None, None, None
        musicbrainz: MusicbrainzProvider = cast("MusicbrainzProvider", musicbrainz_provider)

        mb_result, swapped = await self._search_musicbrainz_with_variants(
            musicbrainz, artist_name, clean_track_name
        )

        if not mb_result:
            self.logger.debug("No MusicBrainz match for '%s - %s'", artist_name, clean_track_name)
            # No MB match, try library artist as fallback
            if metadata := await self._get_library_artist_metadata(artist_name):
                return metadata, f"library artist '{artist_name}'", artist_name, clean_track_name
            return None, None, None, None

        mb_artist, mb_release_groups = mb_result
        if swapped:
            # Swap the variables so subsequent lookups use the correct order
            artist_name, clean_track_name = clean_track_name, artist_name
            self.logger.debug(
                "MusicBrainz matched with swapped artist/track: '%s - %s'",
                artist_name,
                clean_track_name,
            )

        # Prefer single artwork (exact track art), then fall back to album artwork
        singles = [rg for rg in mb_release_groups if rg.primary_type == "Single"]
        albums = [rg for rg in mb_release_groups if rg.primary_type == "Album"]

        for mb_release_group in singles:
            if result := await self._get_release_group_artwork(mb_release_group):
                thumb, provider_name = result
                return (
                    thumb,
                    f"single '{mb_release_group.title}' via {provider_name}",
                    artist_name,
                    clean_track_name,
                )

        if singles:
            self.logger.debug(
                "No artwork found for single release of '%s - %s', trying album artwork",
                artist_name,
                clean_track_name,
            )

        for mb_release_group in albums:
            if result := await self._get_release_group_artwork(mb_release_group):
                thumb, provider_name = result
                return (
                    thumb,
                    f"album '{mb_release_group.title}' via {provider_name}",
                    artist_name,
                    clean_track_name,
                )

        # Log when falling back to artist artwork
        self.logger.debug(
            "No album artwork for '%s - %s', trying artist artwork",
            artist_name,
            clean_track_name,
        )

        # Check library for artist before external lookup
        if metadata := await self._get_library_artist_metadata(mb_artist.name):
            return metadata, f"library artist '{mb_artist.name}'", artist_name, clean_track_name

        # Fall back to external artist artwork
        temp_artist = Artist(
            item_id="temp",
            provider="temp",
            name=mb_artist.name,
            provider_mappings=set(),
        )
        temp_artist.mbid = mb_artist.id
        for provider in self.providers:
            if ProviderFeature.ARTIST_METADATA not in provider.supported_features:
                continue
            try:
                if artist_metadata := await provider.get_artist_metadata(temp_artist):
                    if artist_thumb := self._get_thumb_image(artist_metadata):
                        return (
                            artist_thumb,
                            f"artist '{mb_artist.name}' via {provider.name}",
                            artist_name,
                            clean_track_name,
                        )
            except (
                ProviderUnavailableError,
                ResourceTemporarilyUnavailable,
                InvalidDataError,
            ):
                pass

        return None, None, None, None

    def _get_thumb_image(self, metadata: MediaItemMetadata) -> MediaItemMetadata | None:
        """
        Extract only THUMB type image from metadata.

        Returns new metadata with only the thumb image, or None if no thumb found.
        Used for radio artwork where we specifically need artist/album thumbnails,
        not logos or banners.

        :param metadata: Metadata to extract thumb from.
        """
        if not metadata.images:
            return None
        for img in metadata.images:
            if img.type == ImageType.THUMB:
                return MediaItemMetadata(images=UniqueList([img]))
        return None

    async def _get_library_track_metadata(
        self, artist_name: str, track_name: str
    ) -> MediaItemMetadata | None:
        """
        Search library for matching track and return its metadata.

        :param artist_name: Artist name to match.
        :param track_name: Track title to match.
        """
        try:
            search_query = f"{artist_name} {track_name}"
            library_tracks = await self.mass.music.tracks.search(search_query, "library", limit=5)
            for track in library_tracks:
                if not self._match_artist_name(artist_name, track.artists):
                    continue
                if not compare_strings(track_name, track.name, strict=False):
                    continue
                if image_url := await self._get_library_item_thumb(track):
                    return MediaItemMetadata(
                        images=UniqueList(
                            [
                                MediaItemImage(
                                    type=ImageType.THUMB,
                                    path=image_url,
                                    provider="library",
                                    remotely_accessible=True,
                                )
                            ]
                        )
                    )
        except InvalidDataError:
            pass
        return None

    async def _get_library_artist_metadata(self, artist_name: str) -> MediaItemMetadata | None:
        """
        Search library for matching artist and return its metadata.

        :param artist_name: Artist name to match.
        """
        try:
            library_artists = await self.mass.music.artists.search(artist_name, "library", limit=5)
            for artist in library_artists:
                if not compare_strings(artist_name, artist.name, strict=False):
                    continue
                if artist.metadata and artist.metadata.images:
                    for img in artist.metadata.images:
                        if img.type == ImageType.THUMB:
                            return MediaItemMetadata(
                                images=UniqueList(
                                    [
                                        MediaItemImage(
                                            type=ImageType.THUMB,
                                            path=self.get_image_url(img, prefer_proxy=True),
                                            provider="library",
                                            remotely_accessible=True,
                                        )
                                    ]
                                )
                            )
        except InvalidDataError:
            pass
        return None

    def _match_artist_name(self, search_name: str, artists: list[Artist | ItemMapping]) -> bool:
        """
        Check if any artist matches the search name.

        :param search_name: Artist name to search for.
        :param artists: List of artists to check against.
        """
        for artist in artists:
            if compare_strings(search_name, artist.name, strict=False):
                return True
            # Handle "The" prefix variations
            if compare_strings(f"The {search_name}", artist.name, strict=False):
                return True
            if artist.name.lower().startswith("the "):
                if compare_strings(search_name, artist.name[4:], strict=False):
                    return True
        return False

    async def _get_library_item_thumb(self, track: Track) -> str | None:
        """
        Get image URL for library track with fallback: track -> album -> artist.

        :param track: Track to get image for.
        """
        # Try track image
        if track.metadata and track.metadata.images:
            for img in track.metadata.images:
                if img.type == ImageType.THUMB:
                    return self.get_image_url(img, prefer_proxy=True)

        # Try album image
        if track.album:
            album = track.album
            if isinstance(album, ItemMapping):
                try:
                    full_album = await self.mass.music.albums.get_library_item(album.item_id)
                    if full_album and full_album.metadata and full_album.metadata.images:
                        for img in full_album.metadata.images:
                            if img.type == ImageType.THUMB:
                                return self.get_image_url(img, prefer_proxy=True)
                except MediaNotFoundError:
                    pass
            elif isinstance(album, Album) and album.metadata and album.metadata.images:
                for img in album.metadata.images:
                    if img.type == ImageType.THUMB:
                        return self.get_image_url(img, prefer_proxy=True)

        # Try artist image
        for artist in track.artists:
            if isinstance(artist, ItemMapping):
                try:
                    full_artist = await self.mass.music.artists.get_library_item(artist.item_id)
                    if full_artist and full_artist.metadata and full_artist.metadata.images:
                        for img in full_artist.metadata.images:
                            if img.type == ImageType.THUMB:
                                return self.get_image_url(img, prefer_proxy=True)
                except MediaNotFoundError:
                    pass
            elif isinstance(artist, Artist) and artist.metadata and artist.metadata.images:
                for img in artist.metadata.images:
                    if img.type == ImageType.THUMB:
                        return self.get_image_url(img, prefer_proxy=True)

        return None

    def get_radio_stream_station_image(self, streamdetails: StreamDetails) -> str | None:
        """
        Get station image URL from queue current item.

        :param streamdetails: StreamDetails for the radio stream.
        """
        if streamdetails.queue_id and (
            queue := self.mass.player_queues.get(streamdetails.queue_id)
        ):
            if queue.current_item and queue.current_item.media_item:
                if station_image := queue.current_item.media_item.image:
                    return self.get_image_url(station_image)
        return None

    @staticmethod
    def normalize_radio_artist_name(artist_name: str) -> str:
        """
        Normalize artist name from radio stream metadata.

        Handles common formats like "Squier, Billy" -> "Billy Squier" while
        avoiding mangling of names like "Lipps, Inc." or "Portugal. The Man".

        :param artist_name: Raw artist name to normalize.
        """
        # Business/title suffixes that should not be flipped
        no_flip_suffixes = ("inc", "inc.", "ltd", "ltd.", "llc", "corp")
        # Specific known bands that are 2 words total and split by a comma
        valid_artist_names = {
            "hello, goodbye",
            "wait, what",
            "goodnight, sunrise",
            "slaughter beach, dog",
            "mount, eerie",
            "american, native",
        }

        normalized = artist_name.replace("_", " ")

        if "," not in normalized:
            return normalized

        # Check against known artist exceptions first
        if normalized.lower() in valid_artist_names:
            return normalized

        # Don't flip if contains "and" or "&" (e.g., "Crosby, Stills & Nash")
        if " and " in normalized.lower() or " & " in normalized:
            return normalized

        parts = normalized.split(",", 1)
        if len(parts) != 2:
            return normalized

        before_comma = parts[0].strip()
        after_comma = parts[1].strip()
        after_comma_lower = after_comma.lower()

        # Don't flip if suffix is a business/title term
        if after_comma_lower in no_flip_suffixes:
            return normalized

        # Flip if suffix is exactly "The" (e.g., "Beatles, The" -> "The Beatles")
        if after_comma_lower == "the":
            return f"{after_comma} {before_comma}"

        # Don't flip if 2+ words after comma (e.g., "Portugal, The Man")
        if len(after_comma.split()) >= 2:
            return normalized

        # Standard flip (e.g., "Squier, Billy" -> "Billy Squier")
        return f"{after_comma} {before_comma}"

    async def get_image_url_by_name(
        self,
        artist_name: str,
        track_name: str,
        fallback_image_url: str | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """
        Look up artwork by artist and track name.

        Searches library and external providers for matching artwork.
        Also returns corrected artist/track names if the search detects
        swapped metadata (e.g., "Track - Artist" instead of "Artist - Track").

        :param artist_name: Artist name to search for.
        :param track_name: Track title to search for.
        :param fallback_image_url: Fallback image URL if no artwork found.
        :returns: Tuple of (image_url, corrected_artist, corrected_track).
        """
        if " / " in artist_name:
            artist_name = artist_name.split(" / ", 1)[0].strip()
        else:
            artists_tuple = split_artists(artist_name)
            artist_name = artists_tuple[0] if artists_tuple else artist_name

        if any(phrase in artist_name.lower() for phrase in AD_DETECTION_PHRASES):
            return fallback_image_url, None, None

        cache_key = f"{artist_name.lower()}|{track_name.lower()}"
        cached_result = await self.mass.cache.get(
            key=cache_key,
            category=CACHE_CATEGORY_RADIO_ARTWORK,
        )
        if cached_result is not None:
            if cached_result != "":
                self.logger.debug(
                    "Radio artwork for '%s - %s': cached",
                    artist_name,
                    track_name,
                )
                return str(cached_result), None, None
            self.logger.debug(
                "Radio artwork for '%s - %s': cached miss",
                artist_name,
                track_name,
            )
            return fallback_image_url, None, None

        image_url = None
        corrected_artist = None
        corrected_track = None
        try:
            (
                metadata,
                source,
                corrected_artist,
                corrected_track,
            ) = await self.get_track_metadata_by_name(
                artist_name=artist_name,
                track_name=track_name,
            )
            # Use corrected artist/track for logging if available (handles swapped metadata)
            log_artist = corrected_artist or artist_name
            log_track = corrected_track or track_name
            if metadata and metadata.images:
                image_url = metadata.images[0].path
                self.logger.debug(
                    "Radio artwork found for '%s - %s': %s",
                    log_artist,
                    log_track,
                    source,
                )
                if "imageproxy" not in image_url:
                    await self.mass.cache.set(
                        key=cache_key,
                        data=image_url,
                        expiration=CACHE_EXPIRATION_RADIO_ARTWORK,
                        category=CACHE_CATEGORY_RADIO_ARTWORK,
                    )
            else:
                self.logger.debug(
                    "Radio artwork for '%s - %s': not found",
                    log_artist,
                    log_track,
                )
                await self.mass.cache.set(
                    key=cache_key,
                    data="",
                    expiration=CACHE_EXPIRATION_RADIO_ARTWORK_MISS,
                    category=CACHE_CATEGORY_RADIO_ARTWORK,
                )
        except (ProviderUnavailableError, ResourceTemporarilyUnavailable, InvalidDataError):
            pass

        return image_url or fallback_image_url, corrected_artist, corrected_track

    async def update_radio_stream_artwork(self, streamdetails: StreamDetails) -> None:
        """
        Fetch and update radio stream artwork.

        :param streamdetails: StreamDetails to update with artwork.
        """
        if not self.mass.config.get_raw_core_config_value(
            self.domain, CONF_ENABLE_RADIO_METADATA_LOOKUP, True
        ):
            return
        if not streamdetails.stream_metadata:
            return
        if not streamdetails.stream_metadata.artist or not streamdetails.stream_metadata.title:
            return

        try:
            fallback_url = streamdetails.stream_metadata.image_url
            original_artist = streamdetails.stream_metadata.artist
            original_title = streamdetails.stream_metadata.title
            image_url, corrected_artist, corrected_track = await self.get_image_url_by_name(
                artist_name=original_artist,
                track_name=original_title,
                fallback_image_url=fallback_url,
            )
            # Use corrected artist/track if metadata was swapped
            final_artist = corrected_artist or original_artist
            final_title = corrected_track or original_title
            if (
                image_url != fallback_url
                or final_artist != original_artist
                or final_title != original_title
            ):
                streamdetails.stream_metadata = StreamMetadata(
                    title=final_title,
                    artist=final_artist,
                    image_url=image_url,
                )
                streamdetails.stream_metadata_last_updated = time()
                if streamdetails.queue_id:
                    self.mass.player_queues.signal_update(streamdetails.queue_id)
        except MusicAssistantError:
            pass

    async def _update_artist_metadata(self, artist: Artist, force_refresh: bool = False) -> None:
        """Get/update rich metadata for an artist."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (artist.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Artist %s", artist.name)
        unique_keys: set[str] = set()

        # The bio is re-derived from the providers on every refresh. Each provider's
        # description is collected as a (language, text) candidate and excluded from the
        # field merge; _select_description picks the winner below. Candidates are appended
        # in priority order: music providers first, then metadata providers (TADB, Wikipedia).
        prev_description = artist.metadata.description
        prev_description_language = artist.metadata.description_language
        description_candidates: list[tuple[str | None, str]] = []

        # collect (local) metadata from all local providers
        local_provs = get_global_cache_value("non_streaming_providers")
        if TYPE_CHECKING:
            local_provs = cast("set[str]", local_provs)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        for prov_mapping in sorted(
            artist.provider_mappings, key=lambda x: x.priority, reverse=True
        ):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.artists.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                if prov_item.metadata.description:
                    description_candidates.append(
                        (prov_item.metadata.description_language, prov_item.metadata.description)
                    )
                artist.metadata.update(
                    replace(prov_item.metadata, description=None, description_language=None)
                )

        # The musicbrainz ID is mandatory for all metadata lookups
        if not artist.mbid:
            if mbid := await self._get_artist_mbid(artist):
                artist.mbid = mbid

        # don't merge online genres on top of source-supplied ones; propagation-derived
        # genres also count as a local source so they survive metadata refreshes
        prefer_local_genres = self.config.get_value(CONF_PREFER_LOCAL_GENRES) and (
            bool(artist.metadata.genres)
            or await self.mass.music.genres.has_derived_genre_mappings(
                MediaType.ARTIST, artist.item_id
            )
        )

        # collect metadata from all (online)[metadata] providers
        # TODO: Utilize a global (cloud) cache for metadata lookups to save on API calls
        if self.config.get_value(CONF_ENABLE_ONLINE_METADATA) and artist.mbid:
            for provider in self.providers:
                if ProviderFeature.ARTIST_METADATA not in provider.supported_features:
                    continue
                if metadata := await provider.get_artist_metadata(artist):
                    if prefer_local_genres:
                        metadata = replace(metadata, genres=None)
                    if metadata.description:
                        description_candidates.append(
                            (metadata.description_language, metadata.description)
                        )
                        metadata = replace(metadata, description=None, description_language=None)
                    artist.metadata.update(metadata)
                    self.logger.debug(
                        "Fetched metadata for Artist %s on provider %s",
                        artist.name,
                        provider.name,
                    )
        artist.metadata.description, artist.metadata.description_language = (
            self._select_description(
                description_candidates, prev_description, prev_description_language
            )
        )

        # update final item in library database
        # set timestamp, used to determine when this function was last called
        artist.metadata.last_refresh = int(time())
        await self.mass.music.artists.update_item_in_library(artist.item_id, artist)

    def _select_description(
        self,
        candidates: Sequence[tuple[str | None, str]],
        prev_description: str | None,
        prev_description_language: str | None,
    ) -> tuple[str | None, str | None]:
        """
        Return the chosen ``(description, language)`` for the artist this refresh.

        :param candidates: ``(language, text)`` tuples in provider-priority order
            (music providers first, then TADB, then Wikipedia).
        :param prev_description: Bio stored before this refresh.
        :param prev_description_language: Language of the bio stored before this refresh.
        """
        pref = self.preferred_language
        # 1. first candidate in the user's preferred language
        for lang, text in candidates:
            if lang == pref:
                return text, lang
        # 2. keep a stored preferred-language bio rather than downgrade
        if prev_description is not None and prev_description_language == pref:
            return prev_description, prev_description_language
        # 3. English fallback, same priority order
        for lang, text in candidates:
            if lang == "en":
                return text, lang
        # 4. last resort: highest-priority bio in any (incl. unknown) language
        if candidates:
            lang, text = candidates[0]
            return text, lang
        return prev_description, prev_description_language

    async def _update_album_metadata(self, album: Album, force_refresh: bool = False) -> None:
        """Get/update rich metadata for an album."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (album.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Album %s", album.name)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        for prov_mapping in sorted(album.provider_mappings, key=lambda x: x.priority, reverse=True):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.albums.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                album.metadata.update(prov_item.metadata)
                if album.year is None and prov_item.year:
                    album.year = prov_item.year
                if album.album_type == AlbumType.UNKNOWN:
                    album.album_type = prov_item.album_type

        # don't merge online genres on top of source-supplied ones; propagation-derived
        # genres also count as a local source so they survive metadata refreshes
        prefer_local_genres = self.config.get_value(CONF_PREFER_LOCAL_GENRES) and (
            bool(album.metadata.genres)
            or await self.mass.music.genres.has_derived_genre_mappings(
                MediaType.ALBUM, album.item_id
            )
        )

        # collect metadata from all (online) [metadata] providers
        # TODO: Utilize a global (cloud) cache for metadata lookups to save on API calls
        if self.config.get_value(CONF_ENABLE_ONLINE_METADATA):
            for provider in self.providers:
                if ProviderFeature.ALBUM_METADATA not in provider.supported_features:
                    continue
                if metadata := await provider.get_album_metadata(album):
                    if prefer_local_genres:
                        metadata = replace(metadata, genres=None)
                    album.metadata.update(metadata)
                    self.logger.debug(
                        "Fetched metadata for Album %s on provider %s",
                        album.name,
                        provider.name,
                    )
        # update final item in library database
        # set timestamp, used to determine when this function was last called
        album.metadata.last_refresh = int(time())
        await self.mass.music.albums.update_item_in_library(album.item_id, album)

    async def _update_track_metadata(self, track: Track, force_refresh: bool = False) -> None:
        """Get/update rich metadata for a track."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (track.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Track %s", track.name)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        for prov_mapping in sorted(track.provider_mappings, key=lambda x: x.priority, reverse=True):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.tracks.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                track.metadata.update(prov_item.metadata)

        # don't merge online genres on top of source-supplied ones
        prefer_local_genres = self.config.get_value(CONF_PREFER_LOCAL_GENRES) and bool(
            track.metadata.genres
        )

        # collect metadata from all [metadata] providers
        # Only fetch metadata from these sources if force_refresh is set OR
        # if the track needs a refresh (based on REFRESH_INTERVAL) AND
        # online metadata is enabled.
        if (force_refresh or needs_refresh) and self.config.get_value(CONF_ENABLE_ONLINE_METADATA):
            for provider in self.providers:
                if ProviderFeature.TRACK_METADATA not in provider.supported_features:
                    continue

                if metadata := await provider.get_track_metadata(track):
                    if prefer_local_genres:
                        metadata = replace(metadata, genres=None)
                    track.metadata.update(metadata)
                    self.logger.debug(
                        "Fetched metadata for Track %s on provider %s",
                        track.name,
                        provider.name,
                    )
        # set timestamp, used to determine when this function was last called
        track.metadata.last_refresh = int(time())
        # update final item in library database
        await self.mass.music.tracks.update_item_in_library(track.item_id, track)

    async def _update_playlist_metadata(
        self, playlist: Playlist, force_refresh: bool = False
    ) -> None:
        """Get/update rich metadata for a playlist."""
        # collect metadata + create collage images
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (playlist.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return
        self.logger.debug("Updating metadata for Playlist %s", playlist.name)
        playlist.metadata.genres = set()
        all_playlist_tracks_images: list[MediaItemImage] = []
        playlist_genres: dict[str, int] = {}
        # retrieve metadata for the playlist from the tracks (such as genres etc.)
        # TODO: retrieve style/mood ?
        async for track in self.mass.music.playlists.tracks(playlist.item_id, playlist.provider):
            if (
                track.image
                and track.image not in all_playlist_tracks_images
                and (
                    track.image.provider in ("url", "builtin", "http")
                    or self.mass.get_provider(track.image.provider)
                )
            ):
                all_playlist_tracks_images.append(track.image)
            if track.metadata.genres:
                genres = track.metadata.genres
            elif (
                isinstance(track, Track)
                and track.album
                and isinstance(track.album, Album)
                and track.album.metadata.genres
            ):
                genres = track.album.metadata.genres
            else:
                genres = set()
            for genre in genres:
                if genre not in playlist_genres:
                    playlist_genres[genre] = 0
                playlist_genres[genre] += 1
            await asyncio.sleep(0)  # yield to eventloop

        playlist_genres_filtered = {genre for genre, count in playlist_genres.items() if count > 5}
        playlist_genres_filtered = set(list(playlist_genres_filtered)[:8])
        playlist.metadata.genres.update(playlist_genres_filtered)
        # create collage images
        cur_images: list[MediaItemImage] = playlist.metadata.images or []
        new_images = []
        # thumb image
        thumb_image = next((x for x in cur_images if x.type == ImageType.THUMB), None)
        if not thumb_image or self._collage_images_dir in thumb_image.path:
            img_filename = thumb_image.path if thumb_image else f"{uuid4().hex}_thumb.jpg"
            if collage_thumb_image := await self.create_collage_image(
                all_playlist_tracks_images, img_filename
            ):
                new_images.append(collage_thumb_image)
        elif thumb_image:
            # just use old image
            new_images.append(thumb_image)
        # fanart image
        fanart_image = next((x for x in cur_images if x.type == ImageType.FANART), None)
        if not fanart_image or self._collage_images_dir in fanart_image.path:
            img_filename = fanart_image.path if fanart_image else f"{uuid4().hex}_fanart.jpg"
            if collage_fanart_image := await self.create_collage_image(
                all_playlist_tracks_images, img_filename, fanart=True
            ):
                new_images.append(collage_fanart_image)
        elif fanart_image:
            # just use old image
            new_images.append(fanart_image)
        playlist.metadata.images = UniqueList(new_images) if new_images else None
        # set timestamp, used to determine when this function was last called
        playlist.metadata.last_refresh = int(time())
        # update final item in library database
        await self.mass.music.playlists.update_item_in_library(playlist.item_id, playlist)

    async def _update_audiobook_metadata(
        self, audiobook: Audiobook, force_refresh: bool = False
    ) -> None:
        """Get/update rich metadata for an audiobook."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (audiobook.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Audiobook %s", audiobook.name)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        prov_images: UniqueList[MediaItemImage] | None = None
        for prov_mapping in sorted(
            audiobook.provider_mappings, key=lambda x: x.priority, reverse=True
        ):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.audiobooks.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                if prov_images is None and prov_item.metadata.images:
                    prov_images = prov_item.metadata.images
                audiobook.metadata.update(prov_item.metadata)
                if audiobook.publisher is None and prov_item.publisher:
                    audiobook.publisher = prov_item.publisher
                if not audiobook.authors and prov_item.authors:
                    audiobook.authors = prov_item.authors
                if not audiobook.narrators and prov_item.narrators:
                    audiobook.narrators = prov_item.narrators
                if not audiobook.duration and prov_item.duration:
                    audiobook.duration = prov_item.duration

        # no way to select a cover for audiobooks, so replace rather than merge the
        # images to keep it in sync with the provider; revisit if a picker is added
        if prov_images is not None:
            audiobook.metadata.images = prov_images

        # update final item in library database
        # set timestamp, used to determine when this function was last called
        audiobook.metadata.last_refresh = int(time())
        await self.mass.music.audiobooks.update_item_in_library(audiobook.item_id, audiobook)

    async def _update_podcast_metadata(self, podcast: Podcast, force_refresh: bool = False) -> None:
        """Get/update rich metadata for a podcast."""
        # collect metadata from all (online) music + metadata providers
        # NOTE: we only do/allow this every REFRESH_INTERVAL
        needs_refresh = (time() - (podcast.metadata.last_refresh or 0)) > REFRESH_INTERVAL
        if not (force_refresh or needs_refresh):
            return

        self.logger.debug("Updating metadata for Podcast %s", podcast.name)

        # collect metadata from all [music] providers
        # note that we sort the providers by priority so that we always
        # prefer local providers over online providers
        unique_keys: set[str] = set()
        prov_images: UniqueList[MediaItemImage] | None = None
        for prov_mapping in sorted(
            podcast.provider_mappings, key=lambda x: x.priority, reverse=True
        ):
            prov = self.mass.get_provider(
                prov_mapping.provider_instance, provider_type=MusicProvider
            )
            if prov is None:
                continue
            # prefer domain for streaming providers as the catalog is the same across instances
            prov_key = prov.domain if prov.is_streaming_provider else prov.instance_id
            if prov_key in unique_keys:
                continue
            unique_keys.add(prov_key)
            with suppress(MediaNotFoundError):
                prov_item = await self.mass.music.podcasts.get_provider_item(
                    prov_mapping.item_id, prov_mapping.provider_instance
                )
                if prov_images is None and prov_item.metadata.images:
                    prov_images = prov_item.metadata.images
                podcast.metadata.update(prov_item.metadata)
                if podcast.publisher is None and prov_item.publisher:
                    podcast.publisher = prov_item.publisher
                if not podcast.total_episodes and prov_item.total_episodes:
                    podcast.total_episodes = prov_item.total_episodes

        # no way to select a cover for podcasts, so replace rather than merge the
        # images to keep it in sync with the provider; revisit if a picker is added
        if prov_images is not None:
            podcast.metadata.images = prov_images

        # update final item in library database
        # set timestamp, used to determine when this function was last called
        podcast.metadata.last_refresh = int(time())
        await self.mass.music.podcasts.update_item_in_library(podcast.item_id, podcast)

    async def _get_artist_mbid(self, artist: Artist) -> str | None:
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        if artist.mbid:
            return artist.mbid
        if compare_strings(artist.name, VARIOUS_ARTISTS_NAME):
            return VARIOUS_ARTISTS_MBID

        musicbrainz_provider = self.mass.get_provider("musicbrainz")
        if not musicbrainz_provider:
            return None
        musicbrainz: MusicbrainzProvider = cast("MusicbrainzProvider", musicbrainz_provider)
        if TYPE_CHECKING:
            assert isinstance(musicbrainz, MusicbrainzProvider)
        # first try with resource URL (e.g. streaming provider share URL)
        for prov_mapping in artist.provider_mappings:
            if prov_mapping.url and prov_mapping.url.startswith("http"):
                if mb_artist := await musicbrainz.get_artist_details_by_resource_url(
                    prov_mapping.url
                ):
                    return mb_artist.id

        # start lookup of musicbrainz id using artist name, albums and tracks
        ref_albums = await self.mass.music.artists.albums(artist.item_id, artist.provider)
        # prefer the (widely supported) top tracks listing, falling back to all tracks
        ref_tracks = await self.mass.music.artists.top_tracks(artist.item_id, artist.provider)
        if not ref_tracks:
            ref_tracks = await self.mass.music.artists.tracks(artist.item_id, artist.provider)
        # try with (strict) ref track(s), using recording id
        for ref_track in ref_tracks:
            if mb_artist := await musicbrainz.get_artist_details_by_track(artist.name, ref_track):
                return mb_artist.id
        # try with (strict) ref album(s), using releasegroup id
        for ref_album in ref_albums:
            if mb_artist := await musicbrainz.get_artist_details_by_album(artist.name, ref_album):
                return mb_artist.id
        # last resort: track matching by name
        for ref_track in ref_tracks:
            if not ref_track.album:
                continue
            if result := await musicbrainz.search(
                artistname=artist.name,
                albumname=ref_track.album.name,
                trackname=ref_track.name,
                trackversion=ref_track.version,
            ):
                return result[0].id

        # lookup failed
        ref_albums_str = "/".join(x.name for x in ref_albums) or "none"
        ref_tracks_str = "/".join(x.name for x in ref_tracks) or "none"
        self.logger.debug(
            "Unable to get musicbrainz ID for artist %s (albums: %s, tracks: %s)",
            artist.name,
            ref_albums_str,
            ref_tracks_str,
        )
        return None

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
            translation_key="background_task.scan_missing_artist_metadata",
            metadata={"task_domain": "metadata_missing_artist_metadata_scan"},
            allow_retry=True,
        )
        self.mass.tasks.register_scheduled_task(
            task_id=PLAYLIST_METADATA_SCAN_TASK_ID,
            name="Refresh playlist metadata",
            handler=self._refresh_playlist_metadata_batch,
            schedule=desired_schedule,
            translation_key="background_task.refresh_playlist_metadata",
            metadata={"task_domain": "metadata_playlist_metadata_scan"},
            allow_retry=True,
        )
        self.mass.tasks.register_scheduled_task(
            task_id=THUMB_CACHE_CLEANUP_TASK_ID,
            name="Cleanup thumbnail cache",
            handler=self._cleanup_thumb_cache,
            schedule=desired_schedule,
            translation_key="background_task.cleanup_thumbnail_cache",
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
        artists = await self.mass.music.artists.get_library_items_by_query(
            limit=METADATA_SCAN_BATCH_SIZE,
            order_by="random",
            extra_query_parts=[query],
        )
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
            f"json_extract({DB_TABLE_PLAYLISTS}.metadata,'$.last_refresh') ISNULL "
            f"OR json_extract({DB_TABLE_PLAYLISTS}.metadata,'$.last_refresh') < {refresh_before}"
        )
        playlists = await self.mass.music.playlists.get_library_items_by_query(
            limit=METADATA_SCAN_BATCH_SIZE,
            order_by="random",
            extra_query_parts=[query],
        )
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

    async def _persist_image_id(self, image_id: str, provider: str, path: str) -> None:
        """Store an image-id mapping so a later imageproxy request can resolve it."""
        await self.cache.set(
            key=image_id,
            data={"provider": provider, "path": path},
            category=CACHE_CATEGORY_IMAGE_IDS,
            provider=self.domain,
            expiration=_IMAGE_ID_CACHE_TTL,
            persistent=True,
        )
