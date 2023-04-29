"""Helper module for parsing the Deezer API. Also helper for getting audio streams.

This helpers file is an async wrapper around the excellent deezer-python package.
While the deezer-python package does an excellent job at parsing the Deezer results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Deezer provider logic.

CREDITS:
deezer-python: https://github.com/browniebroke/deezer-python by @browniebroke
"""

import asyncio
from dataclasses import dataclass

import deezer


@dataclass
class Credential:
    """Class for storing credentials."""

    def __init__(self, app_id: int, app_secret: str, access_token: str):
        """Set the correct things."""
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = access_token

    app_id: int
    app_secret: str
    access_token: str


async def get_deezer_client(creds: Credential) -> deezer.Client:  # type: ignore
    """
    Return a deezer-python Client.

    If credentials are given the client is authorized.
    If no credentials are given the deezer client is not authorized.

    :param creds: Credentials. If none are given client is not authorized, defaults to None
    :type creds: credential, optional
    """
    if not isinstance(creds, Credential):
        raise TypeError("Creds must be of type credential")

    def _authorize():
        return deezer.Client(
            app_id=creds.app_id, app_secret=creds.app_secret, access_token=creds.access_token
        )

    return await asyncio.to_thread(_authorize)


async def get_artist(client: deezer.Client, artist_id: int) -> deezer.Artist:
    """Async wrapper of the deezer-python get_artist function."""

    def _get_artist():
        artist = client.get_artist(artist_id=artist_id)
        return artist

    return await asyncio.to_thread(_get_artist)


async def get_album(client: deezer.Client, album_id: int) -> deezer.Album:
    """Async wrapper of the deezer-python get_album function."""

    def _get_album():
        album = client.get_album(album_id=album_id)
        return album

    return await asyncio.to_thread(_get_album)


async def get_playlist(client: deezer.Client, playlist_id) -> deezer.Playlist:
    """Async wrapper of the deezer-python get_playlist function."""

    def _get_playlist():
        playlist = client.get_playlist(playlist_id=playlist_id)
        return playlist

    return await asyncio.to_thread(_get_playlist)


async def get_track(client: deezer.Client, track_id: int) -> deezer.Track:
    """Async wrapper of the deezer-python get_track function."""

    def _get_track():
        track = client.get_track(track_id=track_id)
        return track

    return await asyncio.to_thread(_get_track)


async def get_user_artists(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_artists function."""

    def _get_artist():
        artists = client.get_user_artists()
        return artists

    return await asyncio.to_thread(_get_artist)


async def get_user_playlists(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_playlists function."""

    def _get_playlist():
        playlists = client.get_user().get_playlists()
        return playlists

    return await asyncio.to_thread(_get_playlist)


async def get_user_albums(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_albums function."""

    def _get_album():
        albums = client.get_user_albums()
        return albums

    return await asyncio.to_thread(_get_album)


async def get_user_tracks(client: deezer.Client) -> deezer.PaginatedList:
    """Async wrapper of the deezer-python get_user_tracks function."""

    def _get_track():
        tracks = client.get_user_tracks()
        return tracks

    return await asyncio.to_thread(_get_track)


async def add_user_albums(client: deezer.Client, album_id: int) -> bool:
    """Async wrapper of the deezer-python add_user_albums function."""

    def _get_track():
        success = client.add_user_album(album_id=album_id)
        return success

    return await asyncio.to_thread(_get_track)


async def remove_user_albums(client: deezer.Client, album_id: int) -> bool:
    """Async wrapper of the deezer-python remove_user_albums function."""

    def _get_track():
        success = client.remove_user_album(album_id=album_id)
        return success

    return await asyncio.to_thread(_get_track)


async def add_user_tracks(client: deezer.Client, track_id: int) -> bool:
    """Async wrapper of the deezer-python add_user_tracks function."""

    def _get_track():
        success = client.add_user_track(track_id=track_id)
        return success

    return await asyncio.to_thread(_get_track)


async def remove_user_tracks(client: deezer.Client, track_id: int) -> bool:
    """Async wrapper of the deezer-python remove_user_tracks function."""

    def _get_track():
        success = client.remove_user_track(track_id=track_id)
        return success

    return await asyncio.to_thread(_get_track)


async def add_user_artists(client: deezer.Client, artist_id: int) -> bool:
    """Async wrapper of the deezer-python add_user_artists function."""

    def _get_artist():
        success = client.add_user_artist(artist_id=artist_id)
        return success

    return await asyncio.to_thread(_get_artist)


async def remove_user_artists(client: deezer.Client, artist_id: int) -> bool:
    """Async wrapper of the deezer-python remove_user_artists function."""

    def _get_artist():
        success = client.remove_user_artist(artist_id=artist_id)
        return success

    return await asyncio.to_thread(_get_artist)


async def search_album(client: deezer.Client, query: str, limit: int = 5) -> list[deezer.Album]:
    """Async wrapper of the deezer-python search_albums function."""

    def _search():
        result = client.search_albums(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def search_track(client: deezer.Client, query: str, limit: int = 5) -> list[deezer.Track]:
    """Async wrapper of the deezer-python search function."""

    def _search():
        result = client.search(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def search_artist(client: deezer.Client, query: str, limit: int = 5) -> list[deezer.Artist]:
    """Async wrapper of the deezer-python search_artist function."""

    def _search():
        result = client.search_artists(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def search_playlist(
    client: deezer.Client, query: str, limit: int = 5
) -> list[deezer.Playlist]:
    """Async wrapper of the deezer-python search_playlist function."""

    def _search():
        result = client.search_playlists(query=query)[:limit]
        return result

    return await asyncio.to_thread(_search)


async def get_album_from_track(track: deezer.Track) -> deezer.Album:
    """Get track's artist."""

    def _get_album_from_track():
        try:
            return track.get_album()
        except deezer.exceptions.DeezerErrorResponse:
            return None

    return await asyncio.to_thread(_get_album_from_track)


async def get_artist_from_track(track: deezer.Track) -> deezer.Artist:
    """Get track's artist."""

    def _get_artist_from_track():
        return track.get_artist()

    return await asyncio.to_thread(_get_artist_from_track)


async def get_artist_from_album(album: deezer.Album) -> deezer.Artist:
    """Get track's artist."""

    def _get_artist_from_album():
        return album.get_artist()

    return await asyncio.to_thread(_get_artist_from_album)


async def get_albums_by_artist(artist: deezer.Artist) -> deezer.PaginatedList:
    """Get albums by an artist."""

    def _get_albums_by_artist():
        return artist.get_albums()

    return await asyncio.to_thread(_get_albums_by_artist)


async def get_artist_top(artist: deezer.Artist) -> deezer.PaginatedList:
    """Get top tracks by an artist."""

    def _get_artist_top():
        return artist.get_top()

    return await asyncio.to_thread(_get_artist_top)
