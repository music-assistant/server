"""Helper module for parsing the Tidal API.

This helpers file is an async wrapper around the excellent tidalapi package.
While the tidalapi package does an excellent job at parsing the Tidal results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Tidal provider logic.
"""

import asyncio
from copy import copy

import tidalapi


def fetched_user_parse(self, json_obj):
    self.id = json_obj["id"]
    self.first_name = json_obj["firstName"]
    self.last_name = json_obj["lastName"]

    return copy(self)


tidalapi.FetchedUser.parse = fetched_user_parse


async def get_library_artists(session: tidalapi.Session, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Favorites.artists function."""

    def _get_library_artists():
        return tidalapi.Favorites(session, user_id).artists(limit=9999)

    return await asyncio.to_thread(_get_library_artists)


async def get_artist(session: tidalapi.Session, prov_artist_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Artist function."""

    def _get_artist():
        return tidalapi.Artist(session, prov_artist_id)

    return await asyncio.to_thread(_get_artist)


async def get_artist_albums(session: tidalapi.Session, prov_artist_id: str) -> dict[str, str]:
    """Async wrapper around 3 tidalapi functions: Artist.get_albums, Artist.get_albums_ep_singles and Artist.get_albums_other"""

    def _get_artist_albums():
        all_albums = []
        albums = tidalapi.Artist(session, prov_artist_id).get_albums(limit=9999)
        eps_singles = tidalapi.Artist(session, prov_artist_id).get_albums_ep_singles(limit=9999)
        compilations = tidalapi.Artist(session, prov_artist_id).get_albums_other(limit=9999)
        all_albums.extend(albums)
        all_albums.extend(eps_singles)
        all_albums.extend(compilations)
        return all_albums

    return await asyncio.to_thread(_get_artist_albums)


async def get_artist_toptracks(session: tidalapi.Session, prov_artist_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Artist.get_top_tracks function."""

    def _get_artist_toptracks():
        return tidalapi.Artist(session, prov_artist_id).get_top_tracks(limit=10)

    return await asyncio.to_thread(_get_artist_toptracks)


async def get_library_albums(session: tidalapi.Session, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Favorites.albums function."""

    def _get_library_albums():
        return tidalapi.Favorites(session, user_id).albums(limit=9999)

    return await asyncio.to_thread(_get_library_albums)


async def get_album(session: tidalapi.Session, prov_album_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Album function."""

    def _get_album():
        return tidalapi.Album(session, prov_album_id)

    return await asyncio.to_thread(_get_album)


async def get_track(session: tidalapi.Session, prov_track_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Track function."""

    def _get_track():
        return tidalapi.Track(session, prov_track_id)

    return await asyncio.to_thread(_get_track)


async def get_track_url(session: tidalapi.Session, prov_track_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Track.get_url function."""

    def _get_track_url():
        return tidalapi.Track(session, prov_track_id).get_url()

    return await asyncio.to_thread(_get_track_url)


async def get_album_tracks(session: tidalapi.Session, prov_album_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Album.tracks function."""

    def _get_album_tracks():
        return tidalapi.Album(session, prov_album_id).tracks()

    return await asyncio.to_thread(_get_album_tracks)


async def get_library_tracks(session: tidalapi.Session, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Favorites.tracks function."""

    def _get_library_tracks():
        return tidalapi.Favorites(session, user_id).tracks(limit=9999)

    return await asyncio.to_thread(_get_library_tracks)


async def get_library_playlists(session: tidalapi.Session, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi LoggedInUser.playlist_and_favorite_playlists function."""

    def _get_library_playlists():

        return tidalapi.LoggedInUser(session, user_id).playlist_and_favorite_playlists()

    return await asyncio.to_thread(_get_library_playlists)


async def get_playlist(session: tidalapi.Session, prov_playlist_id: str) -> dict[str, str]:
    """Async wrapper around the tidal Playlist function."""

    def _get_playlist():
        return tidalapi.Playlist(session, prov_playlist_id)

    return await asyncio.to_thread(_get_playlist)


async def get_playlist_tracks(session: tidalapi.Session, prov_playlist_id: str) -> dict[str, str]:
    """Async wrapper around the tidal Playlist.tracks function."""

    def _get_playlist_tracks():
        return tidalapi.Playlist(session, prov_playlist_id).tracks(limit=9999)

    return await asyncio.to_thread(_get_playlist_tracks)
