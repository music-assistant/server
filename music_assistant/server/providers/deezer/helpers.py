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


class DeezerClient:
    """Async wrapper of the deezer-python library."""

    _client: deezer.Client
    _creds: Credential
    user: deezer.User

    def __init__(self, creds: Credential, client: deezer.Client):
        """Initialize the client."""
        self._creds = creds
        self._client = client
        self.user = self._client.get_user()

    async def get_deezer_client(self, creds: Credential) -> deezer.Client:  # type: ignore
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

    async def get_artist(self, artist_id: int) -> deezer.Artist:
        """Async wrapper of the deezer-python get_artist function."""

        def _get_artist():
            artist = self._client.get_artist(artist_id=artist_id)
            return artist

        return await asyncio.to_thread(_get_artist)

    async def get_album(self, album_id: int) -> deezer.Album:
        """Async wrapper of the deezer-python get_album function."""

        def _get_album():
            album = self._client.get_album(album_id=album_id)
            return album

        return await asyncio.to_thread(_get_album)

    async def get_playlist(self, playlist_id) -> deezer.Playlist:
        """Async wrapper of the deezer-python get_playlist function."""

        def _get_playlist():
            playlist = self._client.get_playlist(playlist_id=playlist_id)
            return playlist

        return await asyncio.to_thread(_get_playlist)

    async def get_track(self, track_id: int) -> deezer.Track:
        """Async wrapper of the deezer-python get_track function."""

        def _get_track():
            track = self._client.get_track(track_id=track_id)
            return track

        return await asyncio.to_thread(_get_track)

    async def get_user_artists(self) -> deezer.PaginatedList:
        """Async wrapper of the deezer-python get_user_artists function."""

        def _get_artist():
            artists = self._client.get_user_artists()
            return artists

        return await asyncio.to_thread(_get_artist)

    async def get_user_playlists(self) -> deezer.PaginatedList:
        """Async wrapper of the deezer-python get_user_playlists function."""

        def _get_playlist():
            playlists = self._client.get_user().get_playlists()
            return playlists

        return await asyncio.to_thread(_get_playlist)

    async def get_user_albums(self) -> deezer.PaginatedList:
        """Async wrapper of the deezer-python get_user_albums function."""

        def _get_album():
            albums = self._client.get_user_albums()
            return albums

        return await asyncio.to_thread(_get_album)

    async def get_user_tracks(self) -> deezer.PaginatedList:
        """Async wrapper of the deezer-python get_user_tracks function."""

        def _get_track():
            tracks = self._client.get_user_tracks()
            return tracks

        return await asyncio.to_thread(_get_track)

    async def add_user_albums(self, album_id: int) -> bool:
        """Async wrapper of the deezer-python add_user_albums function."""

        def _get_track():
            success = self._client.add_user_album(album_id=album_id)
            return success

        return await asyncio.to_thread(_get_track)

    async def remove_user_albums(self, album_id: int) -> bool:
        """Async wrapper of the deezer-python remove_user_albums function."""

        def _get_track():
            success = self._client.remove_user_album(album_id=album_id)
            return success

        return await asyncio.to_thread(_get_track)

    async def add_user_tracks(self, track_id: int) -> bool:
        """Async wrapper of the deezer-python add_user_tracks function."""

        def _get_track():
            success = self._client.add_user_track(track_id=track_id)
            return success

        return await asyncio.to_thread(_get_track)

    async def remove_user_tracks(self, track_id: int) -> bool:
        """Async wrapper of the deezer-python remove_user_tracks function."""

        def _get_track():
            success = self._client.remove_user_track(track_id=track_id)
            return success

        return await asyncio.to_thread(_get_track)

    async def add_user_artists(self, artist_id: int) -> bool:
        """Async wrapper of the deezer-python add_user_artists function."""

        def _get_artist():
            success = self._client.add_user_artist(artist_id=artist_id)
            return success

        return await asyncio.to_thread(_get_artist)

    async def remove_user_artists(self, artist_id: int) -> bool:
        """Async wrapper of the deezer-python remove_user_artists function."""

        def _get_artist():
            success = self._client.remove_user_artist(artist_id=artist_id)
            return success

        return await asyncio.to_thread(_get_artist)

    async def add_user_playlists(self, playlist_id: int) -> bool:
        """Async wrapper of the deezer-python add_user_playlists function."""

        def _get_playlist():
            success = self._client.add_user_playlist(playlist_id=playlist_id)
            return success

        return await asyncio.to_thread(_get_playlist)

    async def remove_user_playlists(self, playlist_id: int) -> bool:
        """Async wrapper of the deezer-python remove_user_playlists function."""

        def _get_playlist():
            success = self._client.remove_user_playlist(playlist_id=playlist_id)
            return success

        return await asyncio.to_thread(_get_playlist)

    async def search_album(self, query: str, limit: int = 5) -> list[deezer.Album]:
        """Async wrapper of the deezer-python search_albums function."""

        def _search():
            result = self._client.search_albums(query=query)[:limit]
            return result

        return await asyncio.to_thread(_search)

    async def search_track(self, query: str, limit: int = 5) -> list[deezer.Track]:
        """Async wrapper of the deezer-python search function."""

        def _search():
            result = self._client.search(query=query)[:limit]
            return result

        return await asyncio.to_thread(_search)

    async def search_artist(self, query: str, limit: int = 5) -> list[deezer.Artist]:
        """Async wrapper of the deezer-python search_artist function."""

        def _search():
            result = self._client.search_artists(query=query)[:limit]
            return result

        return await asyncio.to_thread(_search)

    async def search_playlist(self, query: str, limit: int = 5) -> list[deezer.Playlist]:
        """Async wrapper of the deezer-python search_playlist function."""

        def _search():
            result = self._client.search_playlists(query=query)[:limit]
            return result

        return await asyncio.to_thread(_search)

    async def get_album_from_track(self, track: deezer.Track) -> deezer.Album:
        """Get track's artist."""

        def _get_album_from_track():
            try:
                return track.get_album()
            except deezer.exceptions.DeezerErrorResponse:
                return None

        return await asyncio.to_thread(_get_album_from_track)

    async def get_artist_from_track(self, track: deezer.Track) -> deezer.Artist:
        """Get track's artist."""

        def _get_artist_from_track():
            return track.get_artist()

        return await asyncio.to_thread(_get_artist_from_track)

    async def get_artist_from_album(self, album: deezer.Album) -> deezer.Artist:
        """Get track's artist."""

        def _get_artist_from_album():
            return album.get_artist()

        return await asyncio.to_thread(_get_artist_from_album)

    async def get_albums_by_artist(self, artist: deezer.Artist) -> deezer.PaginatedList:
        """Get albums by an artist."""

        def _get_albums_by_artist():
            return artist.get_albums()

        return await asyncio.to_thread(_get_albums_by_artist)

    async def get_artist_top(self, artist: deezer.Artist, limit: int = 25) -> deezer.PaginatedList:
        """Get top tracks by an artist."""

        def _get_artist_top():
            return artist.get_top()[:limit]

        return await asyncio.to_thread(_get_artist_top)

    async def get_recommended_tracks(self) -> deezer.PaginatedList:
        """Get recommended tracks for user."""

        def _get_recommended_tracks():
            return self._client.get_user_recommended_tracks()

        return await asyncio.to_thread(_get_recommended_tracks)

    async def create_playlist(self, playlist_name) -> deezer.Playlist:
        """Create a playlist on deezer."""

        def _create_playlist():
            playlist_id = self._client.create_playlist(playlist_name=playlist_name)
            return self._client.get_playlist(playlist_id=playlist_id)

        return await asyncio.to_thread(_create_playlist)

    async def add_playlist_tracks(self, playlist_id: int, tracks: list[int]):
        """Add tracks to playlist."""

        def _add_playlist_tracks():
            playlist = self._client.get_playlist(playlist_id=playlist_id)
            playlist.add_tracks(tracks=tracks)

        return await asyncio.to_thread(_add_playlist_tracks)

    async def remove_playlist_tracks(self, playlist_id: int, tracks: list[int]):
        """Remove tracks from playlist."""

        def _remove_playlist_tracks():
            playlist = self._client.get_playlist(playlist_id=playlist_id)
            playlist.delete_tracks(tracks=tracks)

        return await asyncio.to_thread(_remove_playlist_tracks)
