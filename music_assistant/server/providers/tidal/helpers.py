"""Helper module for parsing the Tidal API.

This helpers file is an async wrapper around the excellent tidalapi package.
While the tidalapi package does an excellent job at parsing the Tidal results,
it is unfortunately not async, which is required for Music Assistant to run smoothly.
This also nicely separates the parsing logic from the Tidal provider logic.
"""

import asyncio
import webbrowser
from copy import copy

import tidalapi

from music_assistant.common.models.enums import MediaType

"""Monkey Patching tidalapi.FetchedUser.parse to remove the broken 
picture_id retrieval. Will be removed once the tidalapi package is updated. """


def fetched_user_parse(self, json_obj):
    self.id = json_obj["id"]
    self.first_name = json_obj["firstName"]
    self.last_name = json_obj["lastName"]

    return copy(self)


tidalapi.FetchedUser.parse = fetched_user_parse


def get_track_radio(self):
    """
    Queries TIDAL for the track radio, which is a mix of tracks that are similar to this track.
    :return: A list of :class:`Tracks <tidalapi.media.Track>`
    """
    params = {"limit": 100}
    return self.requests.map_request(
        "tracks/%s/radio" % self.id, params=params, parse=self.session.parse_track
    )


tidalapi.Track.radio = get_track_radio


async def tidal_session(
    tidal_session, token_type, access_token, refresh_token=None, expiry_time=None
) -> tidalapi.Session:
    """Async wrapper around the tidalapi Session function."""

    def _tidal_session():
        if access_token is None and tidal_session is None:
            session = tidalapi.Session()
            login, future = session.login_oauth()
            webbrowser.open(f"https://{login.verification_uri_complete}")
            result = future.result()
            return session
        elif access_token is not None and tidal_session is None:
            session = tidalapi.Session()
            session.load_oauth_session(token_type, access_token, refresh_token, expiry_time)
            return session
        else:
            tidal_session.load_oauth_session(token_type, access_token, refresh_token, expiry_time)
            return tidal_session

    return await asyncio.to_thread(_tidal_session)


async def get_library_artists(session: tidalapi.Session, user_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Favorites.artists function."""

    def _get_library_artists():
        return tidalapi.Favorites(session, user_id).artists(limit=9999)

    return await asyncio.to_thread(_get_library_artists)


async def library_items_add_remove(
    session: tidalapi.Session, user_id: str, item_id: str, media_type: MediaType, add: bool = True
) -> dict[str, str]:
    """Async wrapper around the tidalapi Favorites.items add/remove function."""

    def _library_items_add_remove():
        if media_type == MediaType.ARTIST:
            if add:
                return tidalapi.Favorites(session, user_id).add_artist(item_id)
            if not add:
                return tidalapi.Favorites(session, user_id).remove_artist(item_id)
        if media_type == MediaType.ALBUM:
            if add:
                return tidalapi.Favorites(session, user_id).add_album(item_id)
            if not add:
                return tidalapi.Favorites(session, user_id).remove_album(item_id)
        if media_type == MediaType.TRACK:
            if add:
                return tidalapi.Favorites(session, user_id).add_track(item_id)
            if not add:
                return tidalapi.Favorites(session, user_id).remove_track(item_id)
        if media_type == MediaType.PLAYLIST:
            if add:
                return tidalapi.Favorites(session, user_id).add_playlist(item_id)
            if not add:
                return tidalapi.Favorites(session, user_id).remove_playlist(item_id)
        return None

    return await asyncio.to_thread(_library_items_add_remove)


async def get_artist(session: tidalapi.Session, prov_artist_id: str) -> dict[str, str]:
    """Async wrapper around the tidalapi Artist function."""

    def _get_artist():
        return tidalapi.Artist(session, prov_artist_id)

    return await asyncio.to_thread(_get_artist)


async def get_artist_albums(session: tidalapi.Session, prov_artist_id: str) -> dict[str, str]:
    """Async wrapper around 3 tidalapi functions: Artist.get_albums,
    Artist.get_albums_ep_singles and Artist.get_albums_other"""

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


async def add_remove_playlist_tracks(
    session: tidalapi.Session, prov_playlist_id: str, track_ids: list[str], add: bool = True
) -> dict[str, str]:
    """Async wrapper around the tidal Playlist.add and Playlist.remove function."""

    def _add_remove_playlist_tracks():
        if add:
            return tidalapi.UserPlaylist(session, prov_playlist_id).add(track_ids)
        if not add:
            for item in track_ids:
                tidalapi.UserPlaylist(session, prov_playlist_id).remove_by_id(int(item))

    return await asyncio.to_thread(_add_remove_playlist_tracks)


async def get_similar_tracks(
    session: tidalapi.Session, prov_track_id, limit: int
) -> dict[str, str]:
    """Async wrapper around the tidal Track.get_similar_tracks function."""

    def _get_similar_tracks():
        return tidalapi.Track(session, media_id=prov_track_id).get_track_radio(limit)

    return await asyncio.to_thread(_get_similar_tracks)


async def search(
    session: tidalapi.Session, query: str, media_types=None, limit=50, offset=0
) -> dict[str, str]:
    """Async wrapper around the tidalapi Search function."""
    search_types = []
    if MediaType.ARTIST in media_types:
        search_types.append(tidalapi.artist.Artist)
    if MediaType.ALBUM in media_types:
        search_types.append(tidalapi.album.Album)
    if MediaType.TRACK in media_types:
        search_types.append(tidalapi.media.Track)
    if MediaType.PLAYLIST in media_types:
        search_types.append(tidalapi.playlist.Playlist)

    models = None
    if search_types:
        models = search_types

    def _search():
        foo = session.search(query, models, limit, offset)
        return foo

    return await asyncio.to_thread(_search)
