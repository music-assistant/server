"""Deezer musicprovider support for MusicAssistant."""
from collections.abc import AsyncGenerator
from time import time

import deezer
from asyncio_throttle.throttler import Throttler

from music_assistant.common.models.config_entries import ConfigEntry, ProviderConfig
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
)
from music_assistant.common.models.errors import LoginFailed
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    Playlist,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.server.models import ProviderInstanceType
from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.server.server import MusicAssistant

from .helpers import (
    Credential,
    add_user_albums,
    add_user_artists,
    add_user_tracks,
    get_album,
    get_albums_by_artist,
    get_artist,
    get_artist_top,
    get_deezer_client,
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
    search_album,
    search_artist,
    search_track,
    update_access_token,
)

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.ALBUM_METADATA,
    ProviderFeature.TRACK_METADATA,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_METADATA,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
)

CONF_APP_ID = "app_id"
CONF_APP_SECRET = "app_secret"
CONF_AUTHORIZATION_CODE = "authorization_code"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = DeezerProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_APP_ID,
            type=ConfigEntryType.STRING,
            label="App id",
            required=True,
            description="The APP ID you grabbed from deezer developer portal",
        ),
        ConfigEntry(
            key=CONF_APP_SECRET,
            type=ConfigEntryType.STRING,
            label="App secret",
            required=True,
            description="The APP SECRET you grabbed from deezer developer portal",
        ),
        ConfigEntry(
            key=CONF_AUTHORIZATION_CODE,
            type=ConfigEntryType.STRING,
            label="Authorization code",
            required=True,
            description="The auth code u got from deezer",
        ),
    )


class DeezerProvider(MusicProvider):
    """Deezer provider support."""

    client: deezer.Client
    creds: Credential
    _throttler: Throttler

    async def handle_setup(self) -> None:
        """Set up the Deezer provider."""
        self._throttler = Throttler(rate_limit=4, period=1)
        self.creds = Credential(
            self.config.get_value(CONF_APP_ID),  # type: ignore
            self.config.get_value(CONF_APP_SECRET),  # type: ignore
            self.config.get_value(CONF_AUTHORIZATION_CODE),  # type: ignore
        )
        try:
            self.creds = await update_access_token(mass=self, creds=self.creds)
            self.client = await get_deezer_client(creds=self.creds)
        except Exception:
            raise LoginFailed("Invalid login credentials")

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def search(
        self, search_query: str, media_types=list[MediaType] | None, limit: int = 5
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        """
        result = SearchResults()
        if media_types and len(media_types) > 0:  # type: ignore
            for media_type in media_types:
                if media_type == MediaType.TRACK:
                    search_results_track = await search_track(
                        client=self.client, query=search_query
                    )
                    index = 0
                    for thing in search_results_track:
                        if index >= limit:
                            break
                        track = await parse_track(self, thing)
                        result.tracks.append(track)
                        index += 1
                elif media_type == MediaType.ARTIST:
                    search_results_artists = await search_artist(
                        client=self.client, query=search_query
                    )
                    index = 0
                    for thing in search_results_artists:
                        if index >= limit:
                            break
                        artist = await parse_artist(self, thing)
                        result.artists.append(artist)
                    index += 1
                elif media_type == MediaType.ALBUM:
                    search_results_album = await search_album(
                        client=self.client, query=search_query
                    )
                    index = 0
                    for thing in search_results_album:
                        if index >= limit:
                            break
                        album = await parse_album(self, thing)
                        result.albums.append(album)
                        index += 1
            return result
        else:
            # Add tracks
            search_results_album = await search_album(client=self.client, query=search_query)
            search_results_track = await search_track(client=self.client, query=search_query)
            search_results_artists = await search_artist(client=self.client, query=search_query)
            index = 0
            for thing in search_results_track:
                if index >= limit:
                    break
                track = await parse_track(self, thing)
                result.tracks.append(track)
                index += 1
            # Add artists
            index = 0
            for thing in search_results_artists:
                if index >= limit:
                    break
                artist = await parse_artist(self, thing)
                result.artists.append(artist)
                index += 1
            # Add albums
            index = 0
            for thing in search_results_album:
                if index >= limit:
                    break
                album = await parse_album(self, thing)
                result.albums.append(album)
                index += 1
            return result

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Deezer."""
        for artist in await get_user_artists(client=self.client):
            yield await parse_artist(mass=self, artist=artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Deezer."""
        for album in await get_user_albums(client=self.client):
            yield await parse_album(mass=self, album=album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from Deezer."""
        for playlist in await get_user_playlists(client=self.client):
            yield await parse_playlist(mass=self, playlist=playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve all library tracks from Deezer."""
        for track in await get_user_tracks(client=self.client):
            yield await parse_track(mass=self, track=track)

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        return await parse_artist(
            mass=self, artist=await get_artist(client=self.client, artist_id=int(prov_artist_id))
        )

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        return await parse_album(
            mass=self, album=await get_album(client=self.client, album_id=int(prov_album_id))
        )

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        return await parse_playlist(
            mass=self,
            playlist=await get_playlist(client=self.client, playlist_id=int(prov_playlist_id)),
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return await parse_track(
            mass=self, track=await get_track(client=self.client, track_id=int(prov_track_id))
        )

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get all albums in a playlist."""
        album = await get_album(client=self.client, album_id=int(prov_album_id))
        tracks = []
        for track in album.tracks:
            tracks.append(await parse_track(mass=self, track=track))
        return tracks

    async def get_playlist_tracks(self, prov_playlist_id: str) -> list[Track]:
        """Get all tracks in a playlist."""
        playlist = await get_playlist(client=self.client, playlist_id=prov_playlist_id)
        tracks = []
        for track in playlist.tracks:
            tracks.append(await parse_track(mass=self, track=track))
        return tracks

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums by an artist."""
        artist = await get_artist(client=self.client, artist_id=int(prov_artist_id))
        albums = []
        for album in await get_albums_by_artist(artist=artist):
            albums.append(await parse_album(mass=self, album=album))
        return albums

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks of an artist."""
        artist = await get_artist(client=self.client, artist_id=int(prov_artist_id))
        tracks = []
        for track in await get_artist_top(artist=artist):
            tracks.append(await parse_track(mass=self, track=track))
        return tracks

    async def library_add(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Add an item to the library."""
        result = False
        if media_type == MediaType.ARTIST:
            result = await add_user_artists(
                artist_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.ALBUM:
            result = await add_user_albums(
                album_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.TRACK:
            result = await add_user_tracks(
                track_id=int(prov_item_id),
                client=self.client,
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
                client=self.client,
            )
        elif media_type == MediaType.ALBUM:
            result = await remove_user_albums(
                album_id=int(prov_item_id),
                client=self.client,
            )
        elif media_type == MediaType.TRACK:
            result = await remove_user_tracks(
                track_id=int(prov_item_id),
                client=self.client,
            )
        else:
            raise NotImplementedError
        return result

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Return the content details for the given track when it will be streamed."""
        track = await get_track(client=self.client, track_id=int(item_id))
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
