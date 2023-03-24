"""Deezer musicprovider support for MusicAssistant."""
from collections.abc import AsyncGenerator
from time import time

import deezer

from music_assistant.common.models.enums import ContentType, MediaType, ProviderFeature
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    Playlist,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.server.models.music_provider import MusicProvider

from .helpers import (
    Credential,
    add_user_albums,
    add_user_artists,
    add_user_tracks,
    get_album,
    get_artist,
    get_playlist,
    get_track,
    get_url,
    get_user_albums,
    get_user_artists,
    get_user_playlists,
    get_user_tracks,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
    remove_user_albums,
    remove_user_artists,
    remove_user_tracks,
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

    async def get_playlist_tracks(self, prov_playlist_id: str) -> list[Track]:
        """Get all tracks in a playlist."""
        playlist = await get_playlist(creds=self.creds, playlist_id=prov_playlist_id)
        tracks = []
        for track in playlist.tracks:
            tracks.append(await parse_track(mass=self, track=track))
        return tracks

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        artist = await get_artist(artist_id=int(prov_artist_id))
        albums = []
        for album in artist.get_albums():
            albums.append(await parse_album(mass=self, album=album))
        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks of an artist."""
        artist = await get_artist(artist_id=int(prov_artist_id))
        tracks = []
        for track in artist.get_top():
            tracks.append(await parse_track(mass=self, track=track))
        return tracks

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await add_user_artists(
                artist_id=int(prov_item_id),
                creds=self.creds,
            )
        elif media_type == MediaType.ALBUM:
            result = await add_user_albums(
                album_id=int(prov_item_id),
                creds=self.creds,
            )
        elif media_type == MediaType.TRACK:
            result = await add_user_tracks(
                track_id=int(prov_item_id),
                creds=self.creds,
            )
        else:
            raise NotImplementedError
        return result

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await remove_user_artists(
                artist_id=int(prov_item_id),
                creds=self.creds,
            )
        elif media_type == MediaType.ALBUM:
            result = await remove_user_albums(
                album_id=int(prov_item_id),
                creds=self.creds,
            )
        elif media_type == MediaType.TRACK:
            result = await remove_user_tracks(
                track_id=int(prov_item_id),
                creds=self.creds,
            )
        else:
            raise NotImplementedError
        return result

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Return the content details for the given track when it will be streamed."""
        track = await get_track(track_id=int(item_id))
        details = StreamDetails(
            provider=self.domain,
            item_id=item_id,
            content_type=ContentType.MP3,
            media_type=MediaType.TRACK,
            stream_title=track.title,
            duration=track.duration,
            expires=time() + 3600,
            direct=await get_url(mass=self, track_id=item_id, creds=self.creds),
        )
        return details
