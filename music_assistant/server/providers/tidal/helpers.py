"""Helper module for parsing the Tidal API.

This helpers file is an async wrapper around the excellent tidalapi package.
While the tidalapi package does an excellent job at parsing the Tidal results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Tidal provider logic.
"""

import asyncio
import webbrowser

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

from music_assistant.common.models.enums import MediaType
from music_assistant.common.models.errors import MediaNotFoundError


async def tidal_session(
    token_type, access_token, refresh_token=None, expiry_time=None
) -> TidalSession:
    """Async wrapper around the tidalapi Session function."""

    def _tidal_session():
        config = TidalConfig(quality=TidalQuality.lossless, item_limit=10000, alac=False)
        session = TidalSession(config=config)
        if access_token is None:
            login, future = session.login_oauth()
            webbrowser.open(f"https://{login.verification_uri_complete}")
            future.result()
            return session
        else:
            session.load_oauth_session(token_type, access_token, refresh_token, expiry_time)
            return session

    return await asyncio.to_thread(_tidal_session)


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
        artist_obj = None
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
        album_obj = None
        try:
            album_obj = TidalAlbum(session, prov_album_id)
        except HTTPError as err:
            raise MediaNotFoundError(f"Album {prov_album_id} not found") from err
        return album_obj

    return await asyncio.to_thread(_get_album)


async def get_track(session: TidalSession, prov_track_id: str) -> TidalTrack:
    """Async wrapper around the tidalapi Track function."""

    def _get_track():
        track_obj = None
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
        playlist_obj = None
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
        foo = session.search(query, models, limit, offset)
        return foo

    return await asyncio.to_thread(_search)
