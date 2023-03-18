"""Tidal musicprovider support for MusicAssistant."""
from __future__ import annotations

import asyncio
import json
import os
import platform
import time
from collections.abc import AsyncGenerator
from datetime import datetime
from json.decoder import JSONDecodeError
from tempfile import gettempdir

import aiohttp
import tidalapi
from asyncio_throttle import Throttler

from music_assistant.common.models.config_entries import ConfigEntryValue, ProviderConfig
from music_assistant.common.models.enums import ProviderFeature, ProviderType
from music_assistant.common.models.errors import LoginFailed, MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    AlbumType,
    Artist,
    BrowseFolder,
    ContentType,
    ImageType,
    MediaItemImage,
    MediaItemType,
    MediaType,
    Playlist,
    ProviderMapping,
    Radio,
    StreamDetails,
    Track,
)
from music_assistant.common.models.provider import ProviderInstance, ProviderManifest
from music_assistant.constants import CONF_USERNAME
from music_assistant.server import MusicAssistant
from music_assistant.server.helpers.app_vars import app_var
from music_assistant.server.helpers.process import AsyncProcess
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import (
    get_album,
    get_library_albums,
    get_library_artists,
    get_library_playlists,
    get_library_tracks,
)

CACHE_DIR = gettempdir()
SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
)


class TidalProvider(MusicProvider):
    """Implementation of a Tidal MusicProvider."""

    _token_type: str | None = None
    _access_token: str | None = None
    _refresh_token: str | None = None
    _expiry_time: datetime | None = None
    _tidal_user: str | None = None
    _tidal_session: tidalapi.Session | None = None

    async def setup(self) -> None:
        """Handle async initialization of the provider."""

        if not self.config.get_value(CONF_USERNAME):
            raise LoginFailed("Invalid login credentials")
        self._throttler = Throttler(rate_limit=1, period=0.1)
        self._cache_dir = CACHE_DIR
        self._ap_workaround = False

        # try to get a token, raise if that fails
        self._cache_dir = os.path.join(CACHE_DIR, self.instance_id)
        # try login which will raise if it fails

        await self.login()

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def login(self) -> dict:
        """Log-in Tidal and return tokeninfo."""
        session = tidalapi.Session()
        if self._access_token:
            session.load_oauth_session(
                self._token_type, self._access_token, self._refresh_token, self._expiry_time
            )
        else:
            session.login_oauth_simple()
            print(session.check_login())
            self._token_type = session.token_type
            self._access_token = session.access_token
            self._refresh_token = session.refresh_token
            self._expiry_time = session.expiry_time
            self._tidal_user = session.user
        self._tidal_session = session
        return None

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Tidal."""
        artists_obj = await get_library_artists(self._tidal_session, self._tidal_user.id)

        for artist in artists_obj:
            yield await self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Tidal."""
        albums_obj = await get_library_albums(self._tidal_session, self._tidal_user.id)
        for album in albums_obj:
            yield await self._parse_album(album)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Tidal."""
        tracks_obj = await get_library_tracks(self._tidal_session, self._tidal_user.id)
        for track in tracks_obj:
            # Library tracks sometimes do not have a valid artist id
            # In that case, call the API for track details based on track id
            # try:
            yield await self._parse_track(track)
            # except InvalidDataError:
            #    track = await self.get_track(track["videoId"])
            #    yield track

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await get_library_playlists(self._tidal_session, self._tidal_user.id)
        for playlist in playlists_obj:
            yield await self._parse_playlist(playlist)

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        album_obj = await get_album(self._tidal_session, prov_album_id)
        return await self._parse_album(album_obj) if album_obj else None

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        album_obj = await get_album(prov_album_id=prov_album_id)
        if not album_obj.tracks:
            return []
        tracks = []
        for idx, track_obj in enumerate(album_obj.tracks, 1):
            track = await self._parse_track(track_obj=track_obj)
            track.disc_number = 0
            track.track_number = idx
            tracks.append(track)
        return tracks

    async def _parse_artist(self, artist_obj):
        """Parse tidal artist object to generic layout."""
        artist_id = None
        artist_id = artist_obj.id
        artist = Artist(item_id=artist_id, provider=self.domain, name=artist_obj.name)
        artist.add_provider_mapping(
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=f"http://www.tidal.com/artist/{artist_id}",
            )
        )
        # if "genres" in artist_obj:
        #    artist.metadata.genres = set(artist_obj["genres"])
        # if artist_obj.get("images"):
        #    for img in artist_obj["images"]:
        # img_url = f"https://resources.tidal.com/images/{album_obj.picture.replace('-', '/')}/320x320.jpg"
        #        if "2a96cbd8b46e442fc41c2b86b821562f" not in img_url:
        picture = artist_obj.picture
        picture_parsed = str(picture).replace("-", "/")
        artist.metadata.images = [
            MediaItemImage(
                ImageType.THUMB,
                f"https://resources.tidal.com/images/{picture_parsed}/320x320.jpg",
            )
        ]
        #            break
        return artist

    async def _parse_album(self, album_obj: dict):
        """Parse spotify album object to generic layout."""
        # name, version = parse_title_and_version(album_obj["name"])
        name = album_obj.name
        version = album_obj.version
        album_id = album_obj.id
        album = Album(item_id=album_id, provider=self.domain, name=name, version=version)
        for artist_obj in album_obj.artists:
            album.artists.append(await self._parse_artist(artist_obj))
        # if album_obj["album_type"] == "single":
        #    album.album_type = AlbumType.SINGLE
        # elif album_obj["album_type"] == "compilation":
        #    album.album_type = AlbumType.COMPILATION
        # elif album_obj["album_type"] == "album":
        album.album_type = AlbumType.ALBUM
        # if "genres" in album_obj:
        #    album.metadata.genre = set(album_obj["genres"])
        # if album_obj.get("images"):
        cover = album_obj.cover
        cover_parsed = str(cover).replace("-", "/")
        album.metadata.images = [
            MediaItemImage(
                ImageType.THUMB,
                f"https://resources.tidal.com/images/{cover_parsed}/320x320.jpg",
            )
        ]
        # if "external_ids" in album_obj and album_obj["external_ids"].get("upc"):
        album.upc = album_obj.universal_product_number
        # if "label" in album_obj:
        #    album.metadata.label = album_obj["label"]
        # if album_obj.get("release_date"):
        album.year = int(album_obj.year)
        album.metadata.copyright = album_obj.copyright
        album.metadata.explicit = album_obj.explicit
        album.add_provider_mapping(
            ProviderMapping(
                item_id=album_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                content_type=ContentType.OGG,
                bit_rate=320,
                url=f"http://www.tidal.com/album/{album_id}",
            )
        )
        return album

    async def _parse_track(self, track_obj, artist=None):
        """Parse tidal track object to generic layout."""
        name = track_obj.name
        version = track_obj.version
        track = Track(
            item_id=track_obj.id,
            provider=self.domain,
            name=name,
            version=version,
            duration=track_obj.duration / 1000,
            disc_number=track_obj.volume_num,
            track_number=track_obj.track_num,
            # position=track_obj.get("position"),
        )
        # if artist:
        track.artists = []
        for track_artist in track_obj.artists:
            artist = await self._parse_artist(track_artist)
            track.artists.append(artist)

        track.metadata.explicit = track_obj.explicit
        """ if "preview_url" in track_obj:
            track.metadata.preview = track_obj["preview_url"]
        if "external_ids" in track_obj and "isrc" in track_obj["external_ids"]:
            track.isrc = track_obj["external_ids"]["isrc"]
        if "album" in track_obj:
            track.album = await self._parse_album(track_obj["album"])
            if track_obj["album"].get("images"):
                track.metadata.images = [
                    MediaItemImage(ImageType.THUMB, track_obj["album"]["images"][0]["url"])
                ]
        if track_obj.get("copyright"):
            track.metadata.copyright = track_obj["copyright"]
        if track_obj.get("explicit"):
            track.metadata.explicit = True
        if track_obj.get("popularity"):
            track.metadata.popularity = track_obj["popularity"] """
        track_id = track_obj.id
        available = track_obj.available
        track.add_provider_mapping(
            ProviderMapping(
                item_id=track_obj.id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                content_type=ContentType.OGG,
                bit_rate=320,
                url=f"http://www.tidal.com/tracks/{track_id}",
                available=available,
            )
        )
        return track

    async def _parse_playlist(self, playlist_obj):
        """Parse tidal playlist object to generic layout."""
        playlist_id = playlist_obj.id
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.domain,
            name=playlist_obj.name,
            owner=playlist_obj.creator.name,
        )
        playlist.add_provider_mapping(
            ProviderMapping(
                item_id=playlist_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=f"http://www.tidal.com/playlists/{playlist_id}",
            )
        )
        is_editable = False
        if playlist_obj.creator.name == "me":
            is_editable = True
        playlist.is_editable = is_editable
        playlist_image = playlist_obj.picture
        playlist_image_parsed = str(playlist_image).replace("-", "/")
        playlist.metadata.images = [
            MediaItemImage(
                ImageType.THUMB,
                f"https://resources.tidal.com/images/{playlist_image_parsed}/320x320.jpg",
            )
        ]
        playlist.metadata.checksum = str(playlist_obj._etag)
        return playlist


"""    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        return await super().get_library_albums()

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        return await super().get_library_tracks()

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        return await super().get_library_playlists()

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        return await super().get_library_radios()

    async def get_artist(self, prov_artist_id: str) -> Artist:
        pass

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        return await super().get_artist_albums(prov_artist_id)

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        return await super().get_artist_toptracks(prov_artist_id)

    async def get_album(self, prov_album_id: str) -> Album:
        return await super().get_album(prov_album_id)

    async def get_track(self, prov_track_id: str) -> Track:
        return await super().get_track(prov_track_id)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        return await super().get_playlist(prov_playlist_id)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        return await super().get_radio(prov_radio_id)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        return await super().get_album_tracks(prov_album_id)

    async def get_playlist_tracks(self, prov_playlist_id: str) -> list[Track]:
        return await super().get_playlist_tracks(prov_playlist_id)

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        return await super().library_add(prov_item_id, media_type)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        return await super().library_remove(prov_item_id, media_type)

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        return await super().add_playlist_tracks(prov_playlist_id, prov_track_ids)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int]
    ) -> None:
        return await super().remove_playlist_tracks(prov_playlist_id, positions_to_remove)

    async def create_playlist(self, name: str) -> Playlist:
        return await super().create_playlist(name)

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        return await super().get_similar_tracks(prov_track_id, limit)

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        pass

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        return await super().get_audio_stream(streamdetails, seek_position)

    async def get_item(self, media_type: MediaType, prov_item_id: str) -> MediaItemType:
        return await super().get_item(media_type, prov_item_id)

    async def browse(self, path: str) -> BrowseFolder:
        return await super().browse(path)

    async def recommendations(self) -> list[BrowseFolder]:
        return await super().recommendations()

    async def sync_library(self, media_types: tuple[MediaType, ...] | None = None) -> None:
        return await super().sync_library(media_types)

    def is_file(self) -> bool:
        return super().is_file()

    def library_supported(self, media_type: MediaType) -> bool:
        return super().library_supported(media_type)

    def library_edit_supported(self, media_type: MediaType) -> bool:
        return super().library_edit_supported(media_type)

    def _get_library_gen(self, media_type: MediaType) -> AsyncGenerator[MediaItemType, None]:
        return super()._get_library_gen(media_type) """
