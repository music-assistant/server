"""All logic for metadata retrieval."""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from base64 import b64encode
from collections.abc import Iterable
from contextlib import suppress
from time import time
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import aiofiles
from aiohttp import web

from music_assistant.common.helpers.global_cache import get_global_cache_value
from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant.common.models.enums import (
    AlbumType,
    ConfigEntryType,
    ImageType,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant.common.models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    Playlist,
    Radio,
    Track,
)
from music_assistant.constants import (
    CONF_LANGUAGE,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PLAYLISTS,
    DB_TABLE_TRACKS,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.server.helpers.api import api_command
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.helpers.images import create_collage, get_image_thumb
from music_assistant.server.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import CoreConfig
    from music_assistant.server.models.metadata_provider import MetadataProvider
    from music_assistant.server.providers.musicbrainz import MusicbrainzProvider

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
    "jp_JP": "Japanese",
    "nl_NL": "Dutch",
    "no_NO": "Norwegian",
    "pl_PL": "Polish",
    "pt_PT": "Portuguese",
    "ro_RO": "Romanian",
    "ru_RU": "Russian",
    "sk_SK": "Slovak",
    "sl_SI": "Slovenian",
    "sr_RS": "Serbian",
    "sv_SE": "Swedish",
    "tr_TR": "Turkish",
}

DEFAULT_LANGUAGE = "en_US"
REFRESH_INTERVAL = 60 * 60 * 24 * 90
MAX_ONLINE_CALLS_PER_RUN = 25
CONF_ENABLE_ONLINE_METADATA = "enable_online_metadata"


class MetaDataController(CoreController):
    """Several helpers to search and store metadata for mediaitems."""

    domain: str = "metadata"
    config: CoreConfig

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self.cache = self.mass.cache
        self._pref_lang: str | None = None
        self.manifest.name = "Metadata controller"
        self.manifest.description = (
            "Music Assistant's core controller which handles all metadata for music."
        )
        self.manifest.icon = "book-information-variant"
        self._scanner_task: asyncio.Task | None = None
        self._online_slots_available = MAX_ONLINE_CALLS_PER_RUN

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
                options=tuple(ConfigValueOption(value, key) for key, value in LOCALES.items()),
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
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self.config = config
        if not self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            # silence PIL logger
            logging.getLogger("PIL").setLevel(logging.WARNING)
        # make sure that our directory with collage images exists
        self._collage_images_dir = os.path.join(self.mass.storage_path, "collage_images")
        if not await asyncio.to_thread(os.path.exists, self._collage_images_dir):
            await asyncio.to_thread(os.mkdir, self._collage_images_dir)

        self.mass.streams.register_dynamic_route("/imageproxy", self.handle_imageproxy)

    async def close(self) -> None:
        """Handle logic on server stop."""
        self.stop_metadata_scanner()
        self.mass.streams.unregister_dynamic_route("/imageproxy")

    @property
    def providers(self) -> list[MetadataProvider]:
        """Return all loaded/running MetadataProviders."""
        if TYPE_CHECKING:
            return cast(list[MetadataProvider], self.mass.get_providers(ProviderType.METADATA))
        return self.mass.get_providers(ProviderType.METADATA)

    @property
    def preferred_language(self) -> str:
        """Return preferred language for metadata (as 2 letter language code 'en')."""
        return self.locale.split("_")[0]

    @property
    def locale(self) -> str:
        """Return preferred language for metadata (as full locale code 'en_EN')."""
        return self.mass.config.get_raw_core_config_value(
            self.domain, CONF_LANGUAGE, DEFAULT_LANGUAGE
        )

    @api_command("metadata/set_default_preferred_language")
    def set_default_preferred_language(self, lang: str) -> None:
        """
        Set the (default) preferred language.

        Reasoning behind this is that the backend can not make a wise choice for the default,
        so relies on some external source that knows better to set this info, like the frontend
        or a streaming provider.
        Can only be set once (by this call or the user).
        """
        if self.mass.config.get_raw_core_config_value(self.domain, CONF_LANGUAGE):
            return  # already set
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
        if isinstance(item, str):
            item = await self.mass.music.get_item_by_uri(item)
        if item.provider != "library":
            # this shouldn't happen but just in case.
            raise RuntimeError("Metadata can only be updated for library items")
        if item.media_type == MediaType.ARTIST:
            await self._update_artist_metadata(item, force_refresh=force_refresh)
        if item.media_type == MediaType.ALBUM:
            await self._update_album_metadata(item, force_refresh=force_refresh)
        if item.media_type == MediaType.TRACK:
            await self._update_track_metadata(item, force_refresh=force_refresh)
        if item.media_type == MediaType.PLAYLIST:
            await self._update_playlist_metadata(item, force_refresh=force_refresh)
        if item.media_type == MediaType.RADIO:
            await self._update_radio_metadata(item, force_refresh=force_refresh)
        return item

    @api_command("metadata/start_scan")
    def start_metadata_scanner(self) -> None:
        """
        Start scanner for (missing) metadata.

        Usually this is triggered by the music controller after finishing a library sync.
        """
        if self._scanner_task and not self._scanner_task.done():
            # already running
            return
        self._scanner_task = self.mass.create_task(self._metadata_scanner())

    @api_command("metadata/stop_scan")
    def stop_metadata_scanner(self) -> None:
        """Stop scanner for (missing) metadata."""
        if self._scanner_task and not self._scanner_task.done():
            self._scanner_task.cancel()
            self._scanner_task = None

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
        return await self.get_thumbnail(img_path, size)

    async def get_image_url_for_item(
        self,
        media_item: MediaItemType,
        img_type: ImageType = ImageType.THUMB,
        resolve: bool = True,
    ) -> str | None:
        """Get url to image for given media media_item."""
        if not media_item:
            return None
        if isinstance(media_item, ItemMapping):
            media_item = await self.mass.music.get_item_by_uri(media_item.uri)
        if media_item and media_item.metadata.images:
            for img in media_item.metadata.images:
                if img.type != img_type:
                    continue
                if img.remotely_accessible and not resolve:
                    continue
                if img.remotely_accessible and resolve:
                    return self.get_image_url(img)
                return img.path

        # retry with track's album
        if media_item.media_type == MediaType.TRACK and media_item.album:
            return await self.get_image_url_for_item(media_item.album, img_type, resolve)

        # try artist instead for albums
        if media_item.media_type == MediaType.ALBUM and media_item.artists:
            return await self.get_image_url_for_item(media_item.artists[0], img_type, resolve)

        # last resort: track artist(s)
        if media_item.media_type == MediaType.TRACK and media_item.artists:
            for artist in media_item.artists:
                return await self.get_image_url_for_item(artist, img_type, resolve)

        return None

    def get_image_url(
        self,
        image: MediaItemImage,
        size: int = 0,
        prefer_proxy: bool = False,
        image_format: str = "png",
    ) -> str:
        """Get (proxied) URL for MediaItemImage."""
        if not image.remotely_accessible or prefer_proxy or size:
            # return imageproxy url for images that need to be resolved
            # the original path is double encoded
            encoded_url = urllib.parse.quote(urllib.parse.quote(image.path))
            return (
                f"{self.mass.streams.base_url}/imageproxy?path={encoded_url}"
                f"&provider={image.provider}&size={size}&fmt={image_format}"
            )
        return image.path

    async def get_thumbnail(
        self,
        path: str,
        provider: str,
        size: int | None = None,
        base64: bool = False,
        image_format: str = "png",
    ) -> bytes | str:
        """Get/create thumbnail image for path (image url or local path)."""
        if not self.mass.get_provider(provider):
            raise ProviderUnavailableError
        thumbnail = await get_image_thumb(
            self.mass, path, size=size, provider=provider, image_format=image_format
        )
        if base64:
            enc_image = b64encode(thumbnail).decode()
            thumbnail = f"data:image/{image_format};base64,{enc_image}"
        return thumbnail

    async def handle_imageproxy(self, request: web.Request) -> web.Response:
        """Handle request for image proxy."""
        path = request.query["path"]
        provider = request.query.get("provider", "builtin")
        if provider in ("url", "file"):
            # temporary for backwards compatibility
            provider = "builtin"
        size = int(request.query.get("size", "0"))
        image_format = request.query.get("fmt", "png")
        if not self.mass.get_provider(provider):
            return web.Response(status=404)
        if "%" in path:
            # assume (double) encoded url, decode it
            path = urllib.parse.unquote(path)
        with suppress(FileNotFoundError):
            image_data = await self.get_thumbnail(
                path, size=size, provider=provider, image_format=image_format
            )
            # we set the cache header to 1 year (forever)
            # assuming that images do not/rarely change
            return web.Response(
                body=image_data,
                headers={"Cache-Control": "max-age=31536000", "Access-Control-Allow-Origin": "*"},
                content_type=f"image/{image_format}",
            )
        return web.Response(status=404)

    async def create_collage_image(
        self,
        images: Iterable[MediaItemImage],
        img_path: str,
        fanart: bool = False,
    ) -> MediaItemImage | None:
        """Create collage thumb/fanart image for (in-library) playlist."""
        if len(images) < 8 and fanart or len(images) < 3:
            # require at least some images otherwise this does not make a lot of sense
            return None
        try:
            # create collage thumb from playlist tracks
            # if playlist has no default image (e.g. a local playlist)
            dimensions = (2500, 1750) if fanart else (1500, 1500)
            img_data = await create_collage(self.mass, images, dimensions)
            # always overwrite existing path
            async with aiofiles.open(img_path, "wb") as _file:
                await _file.write(img_data)
            return MediaItemImage(
                type=ImageType.FANART if fanart else ImageType.THUMB,
                path=img_path,
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

    async def _update_artist_metadata(self, artist: Artist, force_refresh: bool = False) -> None:
        """Get/update rich metadata for an artist."""
        self.logger.debug("Updating metadata for Artist %s", artist.name)
        unique_keys: set[str] = set()
        # collect (local) metadata from all local providers
        local_provs = get_global_cache_value("non_streaming_providers")
        if TYPE_CHECKING:
            local_provs = cast(set[str], local_provs)

        # collect metadata from all (online) music + metadata providers
        # NOTE: we only allow this every REFRESH_INTERVAL and a max amount of calls per day
        # to not overload the music/metadata providers with api calls
        # TODO: Utilize a global (cloud) cache for metadata lookups to save on API calls

        if force_refresh or (
            self._online_slots_available
            and ((time() - (artist.metadata.last_refresh or 0)) > REFRESH_INTERVAL)
        ):
            self._online_slots_available -= 1
            # set timestamp, used to determine when this function was last called
            artist.metadata.last_refresh = int(time())

            # ensure the item is matched to all providers
            await self.mass.music.artists.match_providers(artist)

            # collect metadata from all (streaming) music providers
            # NOTE: local providers have already pushed their metadata in the sync
            for prov_mapping in artist.provider_mappings:
                if (prov := self.mass.get_provider(prov_mapping.provider_instance)) is None:
                    continue
                if prov.lookup_key in unique_keys:
                    continue
                if prov.lookup_key not in local_provs:
                    unique_keys.add(prov.lookup_key)
                with suppress(MediaNotFoundError):
                    prov_item = await self.mass.music.artists.get_provider_item(
                        prov_mapping.item_id, prov_mapping.provider_instance
                    )
                    artist.metadata.update(prov_item.metadata)

            # The musicbrainz ID is mandatory for all metadata lookups
            if not artist.mbid:
                # TODO: Use a global cache/proxy for the MB lookups to save on API calls
                if mbid := await self._get_artist_mbid(artist):
                    artist.mbid = mbid

            # collect metadata from all (online) metadata providers
            if self.config.get_value(CONF_ENABLE_ONLINE_METADATA) and artist.mbid:
                for provider in self.providers:
                    if ProviderFeature.ARTIST_METADATA not in provider.supported_features:
                        continue
                    if metadata := await provider.get_artist_metadata(artist):
                        artist.metadata.update(metadata)
                        self.logger.debug(
                            "Fetched metadata for Artist %s on provider %s",
                            artist.name,
                            provider.name,
                        )
        # update final item in library database
        await self.mass.music.artists.update_item_in_library(artist.item_id, artist)

    async def _update_album_metadata(self, album: Album, force_refresh: bool = False) -> None:
        """Get/update rich metadata for an album."""
        self.logger.debug("Updating metadata for Album %s", album.name)
        # collect (local) metadata from all local music providers
        local_provs = get_global_cache_value("non_streaming_providers")
        if TYPE_CHECKING:
            local_provs = cast(set[str], local_provs)

        # collect metadata from all (online) music + metadata providers
        # NOTE: we only allow this every REFRESH_INTERVAL and a max amount of calls per day
        # to not overload the (free) metadata providers with api calls
        # TODO: Utilize a global (cloud) cache for metadata lookups to save on API calls
        if force_refresh or (
            self._online_slots_available
            and ((time() - (album.metadata.last_refresh or 0)) > REFRESH_INTERVAL)
            and (album.mbid or album.artists)
        ):
            self._online_slots_available -= 1
            # set timestamp, used to determine when this function was last called
            album.metadata.last_refresh = int(time())

            # ensure the item is matched to all providers (will also get other quality versions)
            await self.mass.music.albums.match_providers(album)

            # collect metadata from all (streaming) music providers
            # NOTE: local providers have already pushed their metadata in the sync
            unique_keys: set[str] = set()
            for prov_mapping in album.provider_mappings:
                if (prov := self.mass.get_provider(prov_mapping.provider_instance)) is None:
                    continue
                if prov.lookup_key in unique_keys:
                    continue
                if prov.lookup_key not in local_provs:
                    unique_keys.add(prov.lookup_key)
                with suppress(MediaNotFoundError):
                    prov_item = await self.mass.music.albums.get_provider_item(
                        prov_mapping.item_id, prov_mapping.provider_instance
                    )
                    album.metadata.update(prov_item.metadata)
                    if album.year is None and prov_item.year:
                        album.year = prov_item.year
                    if album.album_type == AlbumType.UNKNOWN:
                        album.album_type = prov_item.album_type

            # collect metadata from all (online) metadata providers
            if self.config.get_value(CONF_ENABLE_ONLINE_METADATA):
                for provider in self.providers:
                    if ProviderFeature.ALBUM_METADATA not in provider.supported_features:
                        continue
                    if metadata := await provider.get_album_metadata(album):
                        album.metadata.update(metadata)
                        self.logger.debug(
                            "Fetched metadata for Album %s on provider %s",
                            album.name,
                            provider.name,
                        )
        # update final item in library database
        await self.mass.music.albums.update_item_in_library(album.item_id, album)

    async def _update_track_metadata(self, track: Track, force_refresh: bool = False) -> None:
        """Get/update rich metadata for a track."""
        unique_keys: set[str] = set()
        # collect metadata from all (online) music/metadata providers
        # NOTE: we only allow this every REFRESH_INTERVAL and a max amount of calls per day
        # to not overload the (free) metadata providers with api calls
        # TODO: Utilize a global (cloud) cache for metadata lookups to save on API calls
        if force_refresh or (
            self._online_slots_available
            and ((time() - (track.metadata.last_refresh or 0)) > REFRESH_INTERVAL)
            and (track.mbid or track.artists or track.album)
        ):
            self.logger.debug("Updating metadata for Track %s", track.name)
            self._online_slots_available -= 1
            # set timestamp, used to determine when this function was last called
            track.metadata.last_refresh = int(time())

            # ensure the item is matched to all providers (will also get other quality versions)
            await self.mass.music.tracks.match_providers(track)

            # collect metadata from all music providers
            # note that we sort the providers by priority so that we always
            # prefer local providers over online providers
            for prov_mapping in sorted(
                track.provider_mappings, key=lambda x: x.priority, reverse=True
            ):
                if (prov := self.mass.get_provider(prov_mapping.provider_instance)) is None:
                    continue
                if prov.lookup_key in unique_keys:
                    continue
                unique_keys.add(prov.lookup_key)
                with suppress(MediaNotFoundError):
                    prov_item = await self.mass.music.tracks.get_provider_item(
                        prov_mapping.item_id, prov_mapping.provider_instance
                    )
                    track.metadata.update(prov_item.metadata)

            # collect metadata from all metadata providers
            # there is only little metadata available for tracks so we only fetch metadata
            # from other sources if the force flag is set
            if force_refresh and self.config.get_value(CONF_ENABLE_ONLINE_METADATA):
                for provider in self.providers:
                    if ProviderFeature.TRACK_METADATA not in provider.supported_features:
                        continue
                    if metadata := await provider.get_track_metadata(track):
                        track.metadata.update(metadata)
                        self.logger.debug(
                            "Fetched metadata for Track %s on provider %s",
                            track.name,
                            provider.name,
                        )
        # update final item in library database
        await self.mass.music.tracks.update_item_in_library(track.item_id, track)

    async def _update_playlist_metadata(
        self, playlist: Playlist, force_refresh: bool = False
    ) -> None:
        """Get/update rich metadata for a playlist."""
        self.logger.debug("Updating metadata for Playlist %s", playlist.name)
        if not force_refresh and (time() - (playlist.metadata.last_refresh or 0)) < (
            60 * 60 * 24 * 5
        ):
            return
        playlist.metadata.genres = set()
        all_playlist_tracks_images = set()
        playlist_genres: dict[str, int] = {}
        # retrieve metadata for the playlist from the tracks (such as genres etc.)
        # TODO: retrieve style/mood ?
        async for track in self.mass.music.playlists.tracks(playlist.item_id, playlist.provider):
            if track.image:
                all_playlist_tracks_images.add(track.image)
            if track.metadata.genres:
                genres = track.metadata.genres
            elif track.album and isinstance(track.album, Album) and track.album.metadata.genres:
                genres = track.album.metadata.genres
            else:
                genres = set()
            for genre in genres:
                if genre not in playlist_genres:
                    playlist_genres[genre] = 0
                playlist_genres[genre] += 1
            await asyncio.sleep(0)  # yield to eventloop

        playlist_genres_filtered = {genre for genre, count in playlist_genres.items() if count > 5}
        playlist.metadata.genres.update(playlist_genres_filtered)
        # create collage images
        cur_images = playlist.metadata.images or []
        new_images = []
        # thumb image
        thumb_image = next((x for x in cur_images if x.type == ImageType.THUMB), None)
        if not thumb_image or self._collage_images_dir in thumb_image.path:
            thumb_image_path = (
                thumb_image.path
                if thumb_image
                else os.path.join(self._collage_images_dir, f"{uuid4().hex}_thumb.jpg")
            )
            if collage_thumb_image := await self.create_collage_image(
                all_playlist_tracks_images, thumb_image_path
            ):
                new_images.append(collage_thumb_image)
        elif thumb_image:
            # just use old image
            new_images.append(thumb_image)
        # fanart image
        fanart_image = next((x for x in cur_images if x.type == ImageType.FANART), None)
        if not fanart_image or self._collage_images_dir in fanart_image.path:
            fanart_image_path = (
                fanart_image.path
                if fanart_image
                else os.path.join(self._collage_images_dir, f"{uuid4().hex}_fanart.jpg")
            )
            if collage_fanart_image := await self.create_collage_image(
                all_playlist_tracks_images, fanart_image_path, fanart=True
            ):
                new_images.append(collage_fanart_image)
        elif fanart_image:
            # just use old image
            new_images.append(fanart_image)
        playlist.metadata.images = new_images
        # set timestamp, used to determine when this function was last called
        playlist.metadata.last_refresh = int(time())
        # update final item in library database
        await self.mass.music.playlists.update_item_in_library(playlist.item_id, playlist)

    async def _update_radio_metadata(self, radio: Radio, force_refresh: bool = False) -> None:
        """Get/update rich metadata for a radio station."""
        if not force_refresh and (time() - (radio.metadata.last_refresh or 0)) < (REFRESH_INTERVAL):
            return
        # NOTE: we do not have any metadata for radio so consider this future proofing ;-)
        radio.metadata.last_refresh = int(time())
        # update final item in library database
        await self.mass.music.radio.update_item_in_library(radio.item_id, radio)

    async def _get_artist_mbid(self, artist: Artist) -> str | None:
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        if artist.mbid:
            return artist.mbid
        if compare_strings(artist.name, VARIOUS_ARTISTS_NAME):
            return VARIOUS_ARTISTS_MBID

        musicbrainz: MusicbrainzProvider = self.mass.get_provider("musicbrainz")
        if TYPE_CHECKING:
            musicbrainz = cast(MusicbrainzProvider, musicbrainz)
        # first try with resource URL (e.g. streaming provider share URL)
        for prov_mapping in artist.provider_mappings:
            if prov_mapping.url and prov_mapping.url.startswith("http"):
                if mb_artist := await musicbrainz.get_artist_details_by_resource_url(
                    prov_mapping.url
                ):
                    return mb_artist.id

        # start lookup of musicbrainz id using artist name, albums and tracks
        ref_albums = await self.mass.music.artists.albums(
            artist.item_id, artist.provider, in_library_only=False
        )
        ref_tracks = await self.mass.music.artists.tracks(
            artist.item_id, artist.provider, in_library_only=False
        )
        # try with (strict) ref track(s), using recording id
        for ref_track in ref_tracks:
            if mb_artist := await musicbrainz.get_artist_details_by_track(artist.name, ref_track):
                return mb_artist.id
        # try with (strict) ref album(s), using releasegroup id
        for ref_album in ref_albums:
            if mb_artist := await musicbrainz.get_artist_details_by_album(artist.name, ref_album):
                return mb_artist.id
        # last restort: track matching by name
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
            "Unable to get musicbrainz ID for artist %s\n"
            " - using lookup-album(s): %s\n"
            " - using lookup-track(s): %s\n",
            artist.name,
            ref_albums_str,
            ref_tracks_str,
        )
        return None

    async def _metadata_scanner(self) -> None:
        """Scanner for (missing) metadata."""
        self.logger.info("Starting metadata scanner")
        self._online_slots_available = MAX_ONLINE_CALLS_PER_RUN
        timestamp = int(time() - 60 * 60 * 24 * 30)
        # ARTISTS metadata refresh
        query = (
            f"json_extract({DB_TABLE_ARTISTS}.metadata,'$.last_refresh') ISNULL "
            f"OR json_extract({DB_TABLE_ARTISTS}.metadata,'$.last_refresh') < {timestamp}"
        )
        for artist in await self.mass.music.artists.library_items(
            limit=50, order_by="random", extra_query=query
        ):
            await self._update_artist_metadata(artist)

        # ALBUMS metadata refresh
        query = (
            f"json_extract({DB_TABLE_ALBUMS}.metadata,'$.last_refresh') ISNULL "
            f"OR json_extract({DB_TABLE_ALBUMS}.metadata,'$.last_refresh') < {timestamp}"
        )
        for album in await self.mass.music.albums.library_items(
            limit=50, order_by="random", extra_query=query
        ):
            await self._update_album_metadata(album)

        # PLAYLISTS metadata refresh
        query = (
            f"json_extract({DB_TABLE_PLAYLISTS}.metadata,'$.last_refresh') ISNULL "
            f"OR json_extract({DB_TABLE_PLAYLISTS}.metadata,'$.last_refresh') < {timestamp}"
        )
        for playlist in await self.mass.music.playlists.library_items(
            limit=50, order_by="random", extra_query=query
        ):
            await self._update_playlist_metadata(playlist)

        # TRACKS metadata refresh
        timestamp = int(time() - 60 * 60 * 24 * 30)
        query = (
            f"json_extract({DB_TABLE_TRACKS}.metadata,'$.last_refresh') ISNULL "
            f"OR json_extract({DB_TABLE_TRACKS}.metadata,'$.last_refresh') < {timestamp}"
        )
        for track in await self.mass.music.tracks.library_items(
            limit=50, order_by="random", extra_query=query
        ):
            await self._update_track_metadata(track)
        self.logger.info("Metadata scanner finished.")
