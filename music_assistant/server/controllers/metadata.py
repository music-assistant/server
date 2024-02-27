"""All logic for metadata retrieval."""

from __future__ import annotations

import asyncio
import logging
import os
import urllib.parse
from base64 import b64encode
from contextlib import suppress
from time import time
from typing import TYPE_CHECKING
from uuid import uuid4

import aiofiles
from aiohttp import web

from music_assistant.common.models.enums import ImageType, MediaType, ProviderFeature, ProviderType
from music_assistant.common.models.errors import MediaNotFoundError
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
    ROOT_LOGGER_NAME,
    VARIOUS_ARTISTS_ID_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.server.helpers.compare import compare_strings
from music_assistant.server.helpers.images import create_collage, get_image_thumb
from music_assistant.server.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import CoreConfig
    from music_assistant.server.models.metadata_provider import MetadataProvider
    from music_assistant.server.providers.musicbrainz import MusicbrainzProvider

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.metadata")


class MetaDataController(CoreController):
    """Several helpers to search and store metadata for mediaitems."""

    domain: str = "metadata"

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self.cache = self.mass.cache
        self._pref_lang: str | None = None
        self.scan_busy: bool = False
        self.manifest.name = "Metadata controller"
        self.manifest.description = (
            "Music Assistant's core controller which handles all metadata for music."
        )
        self.manifest.icon = "book-information-variant"

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self.mass.streams.register_dynamic_route("/imageproxy", self.handle_imageproxy)

    async def close(self) -> None:
        """Handle logic on server stop."""
        self.mass.streams.unregister_dynamic_route("/imageproxy")

    @property
    def providers(self) -> list[MetadataProvider]:
        """Return all loaded/running MetadataProviders."""
        return self.mass.get_providers(ProviderType.METADATA)  # type: ignore[return-value]

    @property
    def preferred_language(self) -> str:
        """Return preferred language for metadata as 2 letter country code (uppercase).

        Defaults to English (EN).
        """
        return self._pref_lang or "EN"

    @preferred_language.setter
    def preferred_language(self, lang: str) -> None:
        """Set preferred language to 2 letter country code.

        Can only be set once.
        """
        if self._pref_lang is None:
            self._pref_lang = lang.upper()

    def start_scan(self) -> None:
        """Start background scan for missing metadata."""

        async def scan_artist_metadata() -> None:
            """Background task that scans for artists missing metadata on filesystem providers."""
            if self.scan_busy:
                return

            LOGGER.debug("Start scan for missing artist metadata")
            self.scan_busy = True
            async for artist in self.mass.music.artists.iter_library_items():
                if artist.metadata.last_refresh is not None:
                    continue
                # most important is to see artist thumb in listings
                # so if that is already present, move on
                # full details can be grabbed later
                if artist.image:
                    continue
                # simply grabbing the full artist will trigger a full fetch
                with suppress(MediaNotFoundError):
                    await self.mass.music.artists.get(artist.item_id, artist.provider, lazy=False)
                # this is slow on purpose to not cause stress on the metadata providers
                await asyncio.sleep(30)
            self.scan_busy = False
            LOGGER.debug("Finished scan for missing artist metadata")

        self.mass.create_task(scan_artist_metadata)

    async def get_artist_metadata(self, artist: Artist) -> None:
        """Get/update rich metadata for an artist."""
        if not artist.mbid:
            # The musicbrainz ID is mandatory for all metadata lookups
            artist.mbid = await self.get_artist_mbid(artist)
        if not artist.mbid:
            return
        # collect metadata from all providers
        for provider in self.providers:
            if ProviderFeature.ARTIST_METADATA not in provider.supported_features:
                continue
            if metadata := await provider.get_artist_metadata(artist):
                artist.metadata.update(metadata)
                LOGGER.debug(
                    "Fetched metadata for Artist %s on provider %s",
                    artist.name,
                    provider.name,
                )
        # set timestamp, used to determine when this function was last called
        artist.metadata.last_refresh = int(time())

    async def get_album_metadata(self, album: Album) -> None:
        """Get/update rich metadata for an album."""
        # ensure the album has a musicbrainz id or artist(s)
        if not (album.mbid or album.artists):
            return
        # collect metadata from all providers
        for provider in self.providers:
            if ProviderFeature.ALBUM_METADATA not in provider.supported_features:
                continue
            if metadata := await provider.get_album_metadata(album):
                album.metadata.update(metadata)
                LOGGER.debug(
                    "Fetched metadata for Album %s on provider %s",
                    album.name,
                    provider.name,
                )
        # set timestamp, used to determine when this function was last called
        album.metadata.last_refresh = int(time())

    async def get_track_metadata(self, track: Track) -> None:
        """Get/update rich metadata for a track."""
        if not (track.album and track.artists):
            return
        # collect metadata from all providers
        for provider in self.providers:
            if ProviderFeature.TRACK_METADATA not in provider.supported_features:
                continue
            if metadata := await provider.get_track_metadata(track):
                track.metadata.update(metadata)
                LOGGER.debug(
                    "Fetched metadata for Track %s on provider %s",
                    track.name,
                    provider.name,
                )
        # set timestamp, used to determine when this function was last called
        track.metadata.last_refresh = int(time())

    async def get_playlist_metadata(self, playlist: Playlist) -> None:
        """Get/update rich metadata for a playlist."""
        # retrieve genres from tracks
        # TODO: retrieve style/mood ?
        playlist.metadata.genres = set()
        images = set()
        try:
            playlist_genres: dict[str, int] = {}
            async for track in self.mass.music.playlists.tracks(
                playlist.item_id, playlist.provider
            ):
                if track.image:
                    images.add(track.image)
                if track.media_type != MediaType.TRACK:
                    # filter out radio items
                    continue
                if not isinstance(track, Track):
                    continue
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

            playlist_genres_filtered = {
                genre for genre, count in playlist_genres.items() if count > 5
            }
            playlist.metadata.genres.update(playlist_genres_filtered)

            # create collage thumb/fanart from playlist tracks
            # if playlist has no default image (e.g. a local playlist)
            if images and (not playlist.image or playlist.image.provider != "url"):
                if playlist.image and self.mass.storage_path in playlist.image.path:
                    # reuse previous created path
                    img_path = playlist.image.path
                else:
                    img_path = os.path.join(self.mass.storage_path, f"{uuid4().hex}.png")
                img_data = await create_collage(self.mass, list(images))
                async with aiofiles.open(img_path, "wb") as _file:
                    await _file.write(img_data)
                playlist.metadata.images = [
                    MediaItemImage(type=ImageType.THUMB, path=img_path, provider="file")
                ]
        except Exception as err:
            LOGGER.warning(
                "Error while creating playlist image: %s",
                str(err),
                exc_info=err if self.logger.isEnabledFor(10) else None,
            )
        # set timestamp, used to determine when this function was last called
        playlist.metadata.last_refresh = int(time())

    async def get_radio_metadata(self, radio: Radio) -> None:
        """Get/update rich metadata for a radio station."""
        # NOTE: we do not have any metadata for radio so consider this future proofing ;-)
        radio.metadata.last_refresh = int(time())

    async def get_artist_mbid(self, artist: Artist) -> str | None:
        """Fetch musicbrainz id by performing search using the artist name, albums and tracks."""
        if compare_strings(artist.name, VARIOUS_ARTISTS_NAME):
            return VARIOUS_ARTISTS_ID_MBID
        ref_albums = await self.mass.music.artists.albums(artist.item_id, artist.provider)
        if len(ref_albums) < 10:
            # fetch reference albums from provider(s) attached to the artist
            for provider_mapping in artist.provider_mappings:
                if provider_mapping.provider_instance == artist.provider:
                    continue
                ref_albums += await self.mass.music.artists.albums(
                    provider_mapping.item_id, provider_mapping.provider_instance
                )
        ref_tracks = await self.mass.music.artists.tracks(artist.item_id, artist.provider)
        if len(ref_tracks) < 10:
            # fetch reference tracks from provider(s) attached to the artist
            for provider_mapping in artist.provider_mappings:
                if provider_mapping.provider_instance == artist.provider:
                    continue
                ref_tracks += await self.mass.music.artists.tracks(
                    provider_mapping.item_id, provider_mapping.provider_instance
                )
        # start lookup of musicbrainz id
        musicbrainz: MusicbrainzProvider = self.mass.get_provider("musicbrainz")
        assert musicbrainz
        if mbid := await musicbrainz.get_musicbrainz_artist_id(
            artist, ref_albums=ref_albums, ref_tracks=ref_tracks
        ):
            return mbid

        # lookup failed
        ref_albums_str = "/".join(x.name for x in ref_albums) or "none"
        ref_tracks_str = "/".join(x.name for x in ref_tracks) or "none"
        LOGGER.debug(
            "Unable to get musicbrainz ID for artist %s\n"
            " - using lookup-album(s): %s\n"
            " - using lookup-track(s): %s\n",
            artist.name,
            ref_albums_str,
            ref_tracks_str,
        )
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
                if img.provider != "url" and not resolve:
                    continue
                if img.provider != "url" and resolve:
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
        if image.provider != "url" or prefer_proxy or size:
            # return imageproxy url for images that need to be resolved
            # the original path is double encoded
            encoded_url = urllib.parse.quote(urllib.parse.quote(image.path))
            return f"{self.mass.streams.base_url}/imageproxy?path={encoded_url}&provider={image.provider}&size={size}&fmt={image_format}"  # noqa: E501
        return image.path

    async def get_thumbnail(
        self,
        path: str,
        size: int | None = None,
        provider: str = "url",
        base64: bool = False,
        image_format: str = "png",
    ) -> bytes | str:
        """Get/create thumbnail image for path (image url or local path)."""
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
        provider = request.query.get("provider", "url")
        size = int(request.query.get("size", "0"))
        image_format = request.query.get("fmt", "png")
        if "%" in path:
            # assume (double) encoded url, decode it
            path = urllib.parse.unquote(path)

        with suppress(FileNotFoundError):
            image_data = await self.get_thumbnail(
                path, size=size, provider=provider, image_format=image_format
            )
            # we set the cache header to 1 year (forever)
            # the client can use the checksum value to refresh when content changes
            return web.Response(
                body=image_data,
                headers={"Cache-Control": "max-age=31536000"},
                content_type=f"image/{image_format}",
            )
        return web.Response(status=404)
