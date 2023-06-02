"""Helper module for parsing the Tidal API.

This helpers file is an async wrapper around the excellent tidalapi package.
While the tidalapi package does an excellent job at parsing the Tidal results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Tidal provider logic.

CREDITS:
tidalapi: https://github.com/tamland/python-tidal
"""

import asyncio
import logging

from requests import HTTPError
from tidalapi import Album as TidalAlbum
from tidalapi import Artist as TidalArtist
from tidalapi import Favorites as TidalFavorites
from tidalapi import LoggedInUser
from tidalapi import Playlist as TidalPlaylist
from tidalapi import Session as TidalSession
from tidalapi import Track as TidalTrack
from tidalapi import UserPlaylist as TidalUserPlaylist

from music_assistant.common.models.enums import MediaType
from music_assistant.common.models.errors import MediaNotFoundError
from music_assistant.constants import ROOT_LOGGER_NAME

DEFAULT_LIMIT = 50
LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.tidal.helpers")


async def get_library_artists(
    session: TidalSession, user_id: str, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> list[TidalArtist]:
    """Async wrapper around the tidalapi Favorites.artists function."""

    def inner() -> list[TidalArtist]:
        return TidalFavorites(session, user_id).artists(limit=limit, offset=offset)

    return await asyncio.to_thread(inner)


async def library_items_add_remove(
    session: TidalSession, user_id: str, item_id: str, media_type: MediaType, add: bool = True
) -> None:
    """Async wrapper around the tidalapi Favorites.items add/remove function."""

    def inner() -> None:
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

    return await asyncio.to_thread(inner)


async def get_artist(session: TidalSession, prov_artist_id: str) -> TidalArtist:
    """Async wrapper around the tidalapi Artist function."""

    def inner() -> TidalArtist:
        try:
            return TidalArtist(session, prov_artist_id)
        except HTTPError as err:
            if err.response.status_code == 404:
                raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err
            raise err

    return await asyncio.to_thread(inner)


async def get_artist_albums(session: TidalSession, prov_artist_id: str) -> list[TidalAlbum]:
    """Async wrapper around 3 tidalapi album functions."""

    def inner() -> list[TidalAlbum]:
        try:
            artist_obj = TidalArtist(session, prov_artist_id)
            all_albums = []
            albums = artist_obj.get_albums(limit=DEFAULT_LIMIT)
            eps_singles = artist_obj.get_albums_ep_singles(limit=DEFAULT_LIMIT)
            compilations = artist_obj.get_albums_other(limit=DEFAULT_LIMIT)
            all_albums.extend(albums)
            all_albums.extend(eps_singles)
            all_albums.extend(compilations)
            return all_albums
        except HTTPError as err:
            if err.response.status_code == 404:
                raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from err
            raise err

    return await asyncio.to_thread(inner)


async def get_artist_toptracks(
    session: TidalSession, prov_artist_id: str, limit: int = 10, offset: int = 0
) -> list[TidalTrack]:
    """Async wrapper around the tidalapi Artist.get_top_tracks function."""

    def inner() -> list[TidalTrack]:
        return TidalArtist(session, prov_artist_id).get_top_tracks(limit=limit, offset=offset)

    return await asyncio.to_thread(inner)


async def get_library_albums(
    session: TidalSession, user_id: str, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> list[TidalAlbum]:
    """Async wrapper around the tidalapi Favorites.albums function."""

    def inner() -> list[TidalAlbum]:
        return TidalFavorites(session, user_id).albums(limit=limit, offset=offset)

    return await asyncio.to_thread(inner)


async def get_album(session: TidalSession, prov_album_id: str) -> TidalAlbum:
    """Async wrapper around the tidalapi Album function."""

    def inner() -> TidalAlbum:
        try:
            return TidalAlbum(session, prov_album_id)
        except HTTPError as err:
            if err.response.status_code == 404:
                raise MediaNotFoundError(f"Album {prov_album_id} not found") from err
            raise err

    return await asyncio.to_thread(inner)


async def get_track(session: TidalSession, prov_track_id: str) -> TidalTrack:
    """Async wrapper around the tidalapi Track function."""

    def inner() -> TidalTrack:
        try:
            return TidalTrack(session, prov_track_id)
        except HTTPError as err:
            if err.response.status_code == 404:
                raise MediaNotFoundError(f"Track {prov_track_id} not found") from err
            raise err

    return await asyncio.to_thread(inner)


async def get_track_url(session: TidalSession, prov_track_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Track.get_url function."""

    def inner() -> dict[str, str]:
        try:
            return TidalTrack(session, prov_track_id).get_url()
        except HTTPError as err:
            if err.response.status_code == 404:
                raise MediaNotFoundError(f"Track {prov_track_id} not found") from err
            raise err

    return await asyncio.to_thread(inner)


async def get_album_tracks(session: TidalSession, prov_album_id: str) -> list[TidalTrack]:
    """Async wrapper around the tidalapi Album.tracks function."""

    def inner() -> list[TidalTrack]:
        try:
            return TidalAlbum(session, prov_album_id).tracks(limit=DEFAULT_LIMIT)
        except HTTPError as err:
            if err.response.status_code == 404:
                raise MediaNotFoundError(f"Album {prov_album_id} not found") from err
            raise err

    return await asyncio.to_thread(inner)


async def get_library_tracks(
    session: TidalSession, user_id: str, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> list[TidalTrack]:
    """Async wrapper around the tidalapi Favorites.tracks function."""

    def inner() -> list[TidalTrack]:
        return TidalFavorites(session, user_id).tracks(limit=limit, offset=offset)

    return await asyncio.to_thread(inner)


async def get_library_playlists(
    session: TidalSession, user_id: str, offset: int = 0
) -> list[TidalPlaylist]:
    """Async wrapper around the tidalapi LoggedInUser.playlist_and_favorite_playlists function."""

    def inner() -> list[TidalPlaylist]:
        return LoggedInUser(session, user_id).playlist_and_favorite_playlists(offset=offset)

    return await asyncio.to_thread(inner)


async def get_playlist(session: TidalSession, prov_playlist_id: str) -> TidalPlaylist:
    """Async wrapper around the tidal Playlist function."""

    def inner() -> TidalPlaylist:
        try:
            return TidalPlaylist(session, prov_playlist_id)
        except HTTPError as err:
            if err.response.status_code == 404:
                raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err
            raise err

    return await asyncio.to_thread(inner)


async def get_playlist_tracks(
    session: TidalSession, prov_playlist_id: str, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> list[TidalTrack]:
    """Async wrapper around the tidal Playlist.tracks function."""

    def inner() -> list[TidalTrack]:
        try:
            return TidalPlaylist(session, prov_playlist_id).tracks(limit=limit, offset=offset)
        except HTTPError as err:
            if err.response.status_code == 404:
                raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found") from err
            raise err

    return await asyncio.to_thread(inner)


async def add_remove_playlist_tracks(
    session: TidalSession, prov_playlist_id: str, track_ids: list[str], add: bool = True
) -> None:
    """Async wrapper around the tidal Playlist.add and Playlist.remove function."""

    def inner() -> None:
        if add:
            return TidalUserPlaylist(session, prov_playlist_id).add(track_ids)
        for item in track_ids:
            TidalUserPlaylist(session, prov_playlist_id).remove_by_id(int(item))

    return await asyncio.to_thread(inner)


async def create_playlist(
    session: TidalSession, user_id: str, title: str, description: str = None
) -> TidalPlaylist:
    """Async wrapper around the tidal LoggedInUser.create_playlist function."""

    def inner() -> TidalPlaylist:
        return LoggedInUser(session, user_id).create_playlist(title, description)

    return await asyncio.to_thread(inner)


async def get_similar_tracks(session: TidalSession, prov_track_id, limit: int) -> list[TidalTrack]:
    """Async wrapper around the tidal Track.get_similar_tracks function."""

    def inner() -> list[TidalTrack]:
        try:
            return TidalTrack(session, prov_track_id).get_track_radio(limit)
        except HTTPError as err:
            if err.response.status_code == 404:
                raise MediaNotFoundError(f"Track {prov_track_id} not found") from err
            raise err

    return await asyncio.to_thread(inner)


async def search(
    session: TidalSession, query: str, media_types=None, limit=50, offset=0
) -> dict[str, str]:
    """Async wrapper around the tidalapi Search function."""

    def inner() -> dict[str, str]:
        search_types = []
        if MediaType.ARTIST in media_types:
            search_types.append(TidalArtist)
        if MediaType.ALBUM in media_types:
            search_types.append(TidalAlbum)
        if MediaType.TRACK in media_types:
            search_types.append(TidalTrack)
        if MediaType.PLAYLIST in media_types:
            search_types.append(TidalPlaylist)

        models = search_types if search_types else None
        return session.search(query, models, limit, offset)

    return await asyncio.to_thread(inner)
