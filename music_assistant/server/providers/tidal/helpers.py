"""Helper module for parsing the Tidal API.

This helpers file is an async wrapper around the excellent tidalapi package.
While the tidalapi package does an excellent job at parsing the Tidal results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Tidal provider logic.

CREDITS:
tidalapi: https://github.com/tamland/python-tidal
"""

import asyncio
from datetime import datetime, timedelta

from requests import HTTPError
from tidalapi import Album as TidalAlbum
from tidalapi import Artist as TidalArtist
from tidalapi import Config as TidalConfig
from tidalapi import Favorites as TidalFavorites
from tidalapi import LoggedInUser
from tidalapi import Playlist as TidalPlaylist
from tidalapi import Quality as TidalQuality
from tidalapi import Session as TidalSession
from tidalapi import Track as TidalTrack
from tidalapi import UserPlaylist as TidalUserPlaylist

from music_assistant.common.helpers.uri import create_uri
from music_assistant.common.helpers.util import create_sort_name
from music_assistant.common.models.config_entries import ProviderConfig
from music_assistant.common.models.enums import AlbumType, ContentType, ImageType, MediaType
from music_assistant.common.models.errors import MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant.server.helpers.auth import AuthenticationHelper
from music_assistant.server.server import MusicAssistant

CONF_AUTH_TOKEN = "auth_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_USER_ID = "user_id"
CONF_EXPIRY_TIME = "expiry_time"


async def get_library_artists(session: TidalSession, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Favorites.artists function."""

    def _get_library_artists():
        return TidalFavorites(session, user_id).artists(limit=9999)

    return await asyncio.to_thread(_get_library_artists)


async def library_items_add_remove(
    session: TidalSession, user_id: str, item_id: str, media_type: MediaType, add: bool = True
) -> None:
    """Async wrapper around the tidalapi Favorites.items add/remove function."""

    def _library_items_add_remove():
        match media_type:
            case MediaType.ARTIST:
                TidalFavorites(session, user_id).add_artist(item_id) if add else TidalFavorites(
                    session, user_id
                ).remove_artist(item_id)
            case MediaType.ALBUM:
                TidalFavorites(session, user_id).add_album(item_id) if add else TidalFavorites(
                    session, user_id
                ).remove_album(item_id)
            case MediaType.TRACK:
                TidalFavorites(session, user_id).add_track(item_id) if add else TidalFavorites(
                    session, user_id
                ).remove_track(item_id)
            case MediaType.PLAYLIST:
                TidalFavorites(session, user_id).add_playlist(item_id) if add else TidalFavorites(
                    session, user_id
                ).remove_playlist(item_id)
            case MediaType.UNKNOWN:
                return

    return await asyncio.to_thread(_library_items_add_remove)


async def get_artist(session: TidalSession, prov_artist_id: str) -> TidalArtist:
    """Async wrapper around the tidalapi Artist function."""

    def _get_artist():
        try:
            artist_obj = TidalArtist(session, prov_artist_id)
        except HTTPError as err:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err
        return artist_obj

    return await asyncio.to_thread(_get_artist)


async def get_artist_albums(session: TidalSession, prov_artist_id: str) -> list[TidalAlbum]:
    """Async wrapper around 3 tidalapi album functions."""

    def _get_artist_albums():
        all_albums = []
        albums = TidalArtist(session, prov_artist_id).get_albums(limit=9999)
        eps_singles = TidalArtist(session, prov_artist_id).get_albums_ep_singles(limit=9999)
        compilations = TidalArtist(session, prov_artist_id).get_albums_other(limit=9999)
        all_albums.extend(albums)
        all_albums.extend(eps_singles)
        all_albums.extend(compilations)
        return all_albums

    return await asyncio.to_thread(_get_artist_albums)


async def get_artist_toptracks(session: TidalSession, prov_artist_id: str) -> list[TidalTrack]:
    """Async wrapper around the tidalapi Artist.get_top_tracks function."""

    def _get_artist_toptracks():
        return TidalArtist(session, prov_artist_id).get_top_tracks(limit=10)

    return await asyncio.to_thread(_get_artist_toptracks)


async def get_library_albums(session: TidalSession, user_id: str) -> list[TidalAlbum]:
    """Async wrapper around the tidalapi Favorites.albums function."""

    def _get_library_albums():
        return TidalFavorites(session, user_id).albums(limit=9999)

    return await asyncio.to_thread(_get_library_albums)


async def get_album(session: TidalSession, prov_album_id: str) -> TidalAlbum:
    """Async wrapper around the tidalapi Album function."""

    def _get_album():
        try:
            album_obj = TidalAlbum(session, prov_album_id)
        except HTTPError as err:
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err
        return album_obj

    return await asyncio.to_thread(_get_album)


async def get_track(session: TidalSession, prov_track_id: str) -> TidalTrack:
    """Async wrapper around the tidalapi Track function."""

    def _get_track():
        try:
            track_obj = TidalTrack(session, prov_track_id)
        except HTTPError as err:
            raise MediaNotFoundError(f"Track {prov_track_id} not found") from err
        return track_obj

    return await asyncio.to_thread(_get_track)


async def get_track_url(session: TidalSession, prov_track_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Track.get_url function."""

    def _get_track_url():
        return TidalTrack(session, prov_track_id).get_url()

    return await asyncio.to_thread(_get_track_url)


async def get_album_tracks(session: TidalSession, prov_album_id: str) -> list[TidalTrack]:
    """Async wrapper around the tidalapi Album.tracks function."""

    def _get_album_tracks():
        return TidalAlbum(session, prov_album_id).tracks()

    return await asyncio.to_thread(_get_album_tracks)


async def get_library_tracks(session: TidalSession, user_id: str) -> list[TidalTrack]:
    """Async wrapper around the tidalapi Favorites.tracks function."""

    def _get_library_tracks():
        return TidalFavorites(session, user_id).tracks(limit=9999)

    return await asyncio.to_thread(_get_library_tracks)


async def get_library_playlists(session: TidalSession, user_id: str) -> list[TidalPlaylist]:
    """Async wrapper around the tidalapi LoggedInUser.playlist_and_favorite_playlists function."""

    def _get_library_playlists():
        return LoggedInUser(session, user_id).playlist_and_favorite_playlists()

    return await asyncio.to_thread(_get_library_playlists)


async def get_playlist(session: TidalSession, prov_playlist_id: str) -> TidalPlaylist:
    """Async wrapper around the tidal Playlist function."""

    def _get_playlist():
        try:
            playlist_obj = TidalPlaylist(session, prov_playlist_id)
        except HTTPError as err:
            raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err
        return playlist_obj

    return await asyncio.to_thread(_get_playlist)


async def get_playlist_tracks(session: TidalSession, prov_playlist_id: str) -> list[TidalTrack]:
    """Async wrapper around the tidal Playlist.tracks function."""

    def _get_playlist_tracks():
        return TidalPlaylist(session, prov_playlist_id).tracks(limit=9999)

    return await asyncio.to_thread(_get_playlist_tracks)


async def add_remove_playlist_tracks(
    session: TidalSession, prov_playlist_id: str, track_ids: list[str], add: bool = True
) -> None:
    """Async wrapper around the tidal Playlist.add and Playlist.remove function."""

    def _add_remove_playlist_tracks():
        if add:
            return TidalUserPlaylist(session, prov_playlist_id).add(track_ids)
        if not add:
            for item in track_ids:
                TidalUserPlaylist(session, prov_playlist_id).remove_by_id(int(item))

    return await asyncio.to_thread(_add_remove_playlist_tracks)


async def create_playlist(
    session: TidalSession, user_id: str, title: str, description: str = None
) -> TidalPlaylist:
    """Async wrapper around the tidal LoggedInUser.create_playlist function."""

    def _create_playlist():
        return LoggedInUser(session, user_id).create_playlist(title, description)

    return await asyncio.to_thread(_create_playlist)


async def get_similar_tracks(session: TidalSession, prov_track_id, limit: int) -> list[TidalTrack]:
    """Async wrapper around the tidal Track.get_similar_tracks function."""

    def _get_similar_tracks():
        return TidalTrack(session, media_id=prov_track_id).get_track_radio(limit)

    return await asyncio.to_thread(_get_similar_tracks)


async def search(
    session: TidalSession, query: str, media_types=None, limit=50, offset=0
) -> dict[str, str]:
    """Async wrapper around the tidalapi Search function."""
    search_types = []
    if MediaType.ARTIST in media_types:
        search_types.append(TidalArtist)
    if MediaType.ALBUM in media_types:
        search_types.append(TidalAlbum)
    if MediaType.TRACK in media_types:
        search_types.append(TidalTrack)
    if MediaType.PLAYLIST in media_types:
        search_types.append(TidalPlaylist)

    models = None
    if search_types:
        models = search_types

    def _search():
        return session.search(query, models, limit, offset)

    return await asyncio.to_thread(_search)


def get_item_mapping(tidal_provider, media_type: MediaType, key: str, name: str) -> ItemMapping:
    """Create a generic item mapping."""
    return ItemMapping(
        media_type,
        key,
        tidal_provider.instance_id,
        name,
        create_uri(media_type, tidal_provider.instance_id, key),
        create_sort_name(tidal_provider.name),
    )


# Parsers


def parse_artist(tidal_provider, artist_obj: TidalArtist) -> Artist:
    """Parse tidal artist object to generic layout."""
    artist_id = artist_obj.id
    artist = Artist(item_id=artist_id, provider=tidal_provider.instance_id, name=artist_obj.name)
    artist.add_provider_mapping(
        ProviderMapping(
            item_id=str(artist_id),
            provider_domain=tidal_provider.domain,
            provider_instance=tidal_provider.instance_id,
            url=f"http://www.tidal.com/artist/{artist_id}",
        )
    )
    artist.metadata = parse_artist_metadata(tidal_provider, artist_obj)
    return artist


def parse_artist_metadata(tidal_provider, artist_obj: TidalArtist) -> MediaItemMetadata:
    """Parse tidal artist object to MA metadata."""
    metadata = MediaItemMetadata()
    image_url = None
    if artist_obj.name != "Various Artists":
        try:
            image_url = artist_obj.image(750)
        except Exception:
            tidal_provider.logger.info(f"Artist {artist_obj.id} has no available picture")
    metadata.images = [
        MediaItemImage(
            ImageType.THUMB,
            image_url,
        )
    ]
    return metadata


def parse_album(tidal_provider, album_obj: TidalAlbum) -> Album:
    """Parse tidal album object to generic layout."""
    name = album_obj.name
    version = album_obj.version if album_obj.version is not None else None
    album_id = album_obj.id
    album = Album(item_id=album_id, provider=tidal_provider.instance_id, name=name, version=version)
    for artist_obj in album_obj.artists:
        album.artists.append(parse_artist(tidal_provider=tidal_provider, artist_obj=artist_obj))
    if album_obj.type == "ALBUM":
        album.album_type = AlbumType.ALBUM
    elif album_obj.type == "COMPILATION":
        album.album_type = AlbumType.COMPILATION
    elif album_obj.type == "EP":
        album.album_type = AlbumType.EP
    elif album_obj.type == "SINGLE":
        album.album_type = AlbumType.SINGLE

    album.upc = album_obj.universal_product_number
    album.year = int(album_obj.year)
    album.add_provider_mapping(
        ProviderMapping(
            item_id=album_id,
            provider_domain=tidal_provider.domain,
            provider_instance=tidal_provider.instance_id,
            content_type=ContentType.FLAC,
            url=f"http://www.tidal.com/album/{album_id}",
        )
    )
    album.metadata = parse_album_metadata(tidal_provider, album_obj)
    return album


def parse_album_metadata(tidal_provider, album_obj: TidalAlbum) -> MediaItemMetadata:
    """Parse tidal album object to MA metadata."""
    metadata = MediaItemMetadata()
    image_url = None
    try:
        image_url = album_obj.image(1280)
    except Exception:
        tidal_provider.logger.info(f"Album {album_obj.id} has no available picture")
    metadata.images = [
        MediaItemImage(
            ImageType.THUMB,
            image_url,
        )
    ]
    metadata.copyright = album_obj.copyright
    metadata.explicit = album_obj.explicit
    metadata.popularity = album_obj.popularity
    return metadata


def parse_track(tidal_provider, track_obj: TidalTrack) -> Track:
    """Parse tidal track object to generic layout."""
    version = track_obj.version if track_obj.version is not None else None
    track_id = str(track_obj.id)
    track = Track(
        item_id=track_id,
        provider=tidal_provider.instance_id,
        name=track_obj.name,
        version=version,
        duration=track_obj.duration,
        disc_number=track_obj.volume_num,
        track_number=track_obj.track_num,
    )
    track.isrc.add(track_obj.isrc)
    track.album = get_item_mapping(
        tidal_provider=tidal_provider,
        media_type=MediaType.ALBUM,
        key=track_obj.album.id,
        name=track_obj.album.name,
    )
    track.artists = []
    for track_artist in track_obj.artists:
        artist = parse_artist(tidal_provider=tidal_provider, artist_obj=track_artist)
        track.artists.append(artist)
    available = track_obj.available
    track.add_provider_mapping(
        ProviderMapping(
            item_id=track_id,
            provider_domain=tidal_provider.domain,
            provider_instance=tidal_provider.instance_id,
            content_type=ContentType.FLAC,
            bit_rate=1411,
            url=f"http://www.tidal.com/tracks/{track_id}",
            available=available,
        )
    )
    track.metadata = parse_track_metadata(tidal_provider, track_obj)
    return track


def parse_track_metadata(tidal_provider, track_obj: TidalTrack) -> MediaItemMetadata:
    """Parse tidal track object to MA metadata."""
    metadata = MediaItemMetadata()
    try:
        metadata.lyrics = track_obj.lyrics().text
    except Exception:
        tidal_provider.logger.info(f"Track {track_obj.id} has no available lyrics")
    metadata.explicit = track_obj.explicit
    metadata.popularity = track_obj.popularity
    metadata.copyright = track_obj.copyright
    return metadata


def parse_playlist(tidal_provider, playlist_obj: TidalPlaylist) -> Playlist:
    """Parse tidal playlist object to generic layout."""
    playlist_id = playlist_obj.id
    creator_id = playlist_obj.creator.id if playlist_obj.creator else None
    creator_name = playlist_obj.creator.name if playlist_obj.creator else "Tidal"
    playlist = Playlist(
        item_id=playlist_id,
        provider=tidal_provider.instance_id,
        name=playlist_obj.name,
        owner=creator_name,
    )
    playlist.add_provider_mapping(
        ProviderMapping(
            item_id=playlist_id,
            provider_domain=tidal_provider.domain,
            provider_instance=tidal_provider.instance_id,
            url=f"http://www.tidal.com/playlists/{playlist_id}",
        )
    )
    is_editable = bool(creator_id and creator_id == tidal_provider._tidal_user_id)
    playlist.is_editable = is_editable
    playlist.metadata = parse_playlist_metadata(tidal_provider, playlist_obj)
    return playlist


def parse_playlist_metadata(tidal_provider, playlist_obj: TidalPlaylist) -> MediaItemMetadata:
    """Parse tidal playlist object to MA metadata."""
    metadata = MediaItemMetadata()
    image_url = None
    try:
        image_url = playlist_obj.image(1080)
    except Exception:
        tidal_provider.logger.info(f"Playlist {playlist_obj.id} has no available picture")
    metadata.images = [
        MediaItemImage(
            ImageType.THUMB,
            image_url,
        )
    ]
    metadata.checksum = str(playlist_obj.last_updated)
    metadata.popularity = playlist_obj.popularity
    return metadata


# Login and session management


async def tidal_code_login(auth_helper: AuthenticationHelper) -> TidalSession:
    """Async wrapper around the tidalapi Session function."""

    def _tidal_code_login():
        config = TidalConfig(quality=TidalQuality.lossless, item_limit=10000, alac=False)
        session = TidalSession(config=config)
        login, future = session.login_oauth()
        auth_helper.send_url(f"https://{login.verification_uri_complete}")
        future.result()
        return session

    return await asyncio.to_thread(_tidal_code_login)


async def tidal_session(
    token_type, access_token, refresh_token=None, expiry_time=None
) -> TidalSession:
    """Async wrapper around the tidalapi Session function."""

    def _tidal_session():
        config = TidalConfig(quality=TidalQuality.lossless, item_limit=10000, alac=False)
        session = TidalSession(config=config)
        session.load_oauth_session(token_type, access_token, refresh_token, expiry_time)
        return session

    return await asyncio.to_thread(_tidal_session)


# Context manager


class TidalSessionManager:
    """Context manager for Tidal session."""

    def __init__(
        self,
        instance_id: str,
        provider_config: ProviderConfig,
        mass: MusicAssistant,
        quality: TidalQuality = TidalQuality.lossless,
    ):
        """Initialize Tidal session manager."""
        self.config = TidalConfig(quality=quality, item_limit=10000, alac=False)
        self.session = TidalSession(config=self.config)
        self.provider_config = provider_config
        self.mass = mass
        self.instance_id = instance_id
        self.token_type = "Bearer"

    async def __aenter__(self) -> TidalSession:
        """Enter context manager."""
        self.session = await self.validate_token_and_refresh()
        return self.session

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self.session = None

    async def validate_token_and_refresh(self) -> TidalSession:
        """Validate token and refresh if needed."""
        if self.session and datetime.fromisoformat(
            self.provider_config.get_value(CONF_EXPIRY_TIME)
        ) > (datetime.now() + timedelta(days=1)):
            return self
        self.session = await tidal_session(
            token_type=self.token_type,
            access_token=self.provider_config.get_value(CONF_AUTH_TOKEN),
            refresh_token=self.provider_config.get_value(CONF_REFRESH_TOKEN),
            expiry_time=datetime.fromisoformat(self.provider_config.get_value(CONF_EXPIRY_TIME)),
        )
        await self.mass.config.set_provider_config_value(
            self.instance_id, CONF_AUTH_TOKEN, self.session.access_token
        )
        await self.mass.config.set_provider_config_value(
            self.instance_id, CONF_REFRESH_TOKEN, self.session.refresh_token
        )
        await self.mass.config.set_provider_config_value(
            self.instance_id, CONF_EXPIRY_TIME, self.session.expiry_time.isoformat()
        )
        return self.session

    async def check_login(self) -> bool:
        """Check if login was successful."""
        return self.session.check_login()
