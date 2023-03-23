"""Deezer musicprovider support for MusicAssistant."""
from collections.abc import AsyncGenerator

import deezer

from music_assistant.common.models.enums import MediaType, ProviderFeature
from music_assistant.common.models.media_items import Album, Artist, Playlist, SearchResults, Track
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import (
    Credential,
    get_album,
    get_artist,
    get_playlist,
    get_track,
    get_user_albums,
    get_user_artists,
    get_user_playlists,
    get_user_tracks,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
    search,
)

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
)


class DeezerProvider(MusicProvider):
    """Deezer provider support."""

    creds: Credential

    async def setup(self) -> None:
        """Set up the Deezer provider."""
        self.creds = Credential(
            self.config.get_value("app_id"),  # type: ignore
            self.config.get_value("app_secret"),  # type: ignore
            self.config.get_value("access_token"),  # type: ignore
        )

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def search(self, search_query: str, media_types=list[MediaType] | None) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        """
        result = SearchResults()

        filter = ""
        if len(media_types) == 1:  # type: ignore
            # Deezer does not support multiple searchtypes,
            # falls back to track only if no type given
            if media_types[0] == MediaType.ARTIST:  # type: ignore
                filter = "artist"
            elif media_types[0] == MediaType.ALBUM:  # type: ignore
                filter = "album"
            else:
                filter = "track"
        search_result = await search(query=search_query, filter=filter)
        for thing in search_result:
            if isinstance(thing, deezer.Track):
                track = await parse_track(self, thing)
                result.tracks.append(track)
            elif isinstance(thing, deezer.Artist):
                artist = await parse_artist(self, thing)
                result.artists.append(artist)
            elif isinstance(thing, deezer.Album):
                album = await parse_album(self, thing)
                result.albums.append(album)
            else:
                raise TypeError(
                    "Something went wrong when searhing. Result was of type: {}".format(
                        thing.type()
                    )
                )
        return result

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Deezer."""
        for artist in await get_user_artists(creds=self.creds):
            yield await parse_artist(mass=self, artist=artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Deezer."""
        for album in await get_user_albums(creds=self.creds):
            yield await parse_album(mass=self, album=album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from Deezer."""
        for playlist in await get_user_playlists(creds=self.creds):
            yield await parse_playlist(mass=self, playlist=playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve all library tracks from Deezer."""
        for track in await get_user_tracks(creds=self.creds):
            yield await parse_track(mass=self, track=track)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return await parse_artist(mass=self, artist=await get_artist(artist_id=int(prov_artist_id)))

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return await parse_album(mass=self, album=await get_album(album_id=int(prov_album_id)))

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        return await parse_playlist(
            mass=self,
            playlist=await get_playlist(creds=self.creds, playlist_id=int(prov_playlist_id)),
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return await parse_track(mass=self, track=await get_track(track_id=int(prov_track_id)))
