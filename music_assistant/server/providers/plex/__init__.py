"""Plex musicprovider support for MusicAssistant."""
from __future__ import annotations

import logging
from asyncio import TaskGroup
from collections.abc import AsyncGenerator, Callable, Coroutine

import plexapi.exceptions
from aiohttp import ClientTimeout
from plexapi.audio import Album as PlexAlbum
from plexapi.audio import Artist as PlexArtist
from plexapi.audio import Playlist as PlexPlaylist
from plexapi.audio import Track as PlexTrack
from plexapi.library import MusicSection as PlexMusicSection
from plexapi.media import AudioStream as PlexAudioStream
from plexapi.media import Media as PlexMedia
from plexapi.media import MediaPart as PlexMediaPart
from plexapi.myplex import MyPlexAccount, MyPlexPinLogin
from plexapi.server import PlexServer

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
)
from music_assistant.common.models.errors import InvalidDataError, LoginFailed, MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItem,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.server import MusicAssistant
from music_assistant.server.helpers.auth import AuthenticationHelper
from music_assistant.server.helpers.tags import parse_tags
from music_assistant.server.models import ProviderInstanceType
from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.server.providers.plex.helpers import get_libraries

CONF_ACTION_AUTH = "auth"
CONF_ACTION_LIBRARY = "library"
CONF_AUTH_TOKEN = "token"
CONF_LIBRARY_ID = "library_id"

FAKE_ARTIST_PREFIX = "_fake://"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if not config.get_value(CONF_AUTH_TOKEN):
        raise LoginFailed("Invalid login credentials")

    prov = PlexProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # config flow auth action/step (authenticate button clicked)
    if action == CONF_ACTION_AUTH:
        async with AuthenticationHelper(mass, values["session_id"]) as auth_helper:
            plex_auth = MyPlexPinLogin(headers={"X-Plex-Product": "Music Assistant"}, oauth=True)
            auth_url = plex_auth.oauthUrl(auth_helper.callback_url)
            await auth_helper.authenticate(auth_url)
            if not plex_auth.checkLogin():
                raise LoginFailed("Authentication to MyPlex failed")
            # set the retrieved token on the values object to pass along
            values[CONF_AUTH_TOKEN] = plex_auth.token

    # config flow auth action/step to pick the library to use
    # because this call is very slow, we only show/calculate the dropdown if we do
    # not yet have this info or we/user invalidated it.
    conf_libraries = ConfigEntry(
        key=CONF_LIBRARY_ID,
        type=ConfigEntryType.STRING,
        label="Library",
        required=True,
        description="The library to connect to (e.g. Music)",
        depends_on=CONF_AUTH_TOKEN,
        action=CONF_ACTION_LIBRARY,
        action_label="Select Plex Music Library",
    )
    if action in (CONF_ACTION_LIBRARY, CONF_ACTION_AUTH):
        token = mass.config.decrypt_string(values.get(CONF_AUTH_TOKEN))
        if not (libraries := await get_libraries(mass, token)):
            raise LoginFailed("Unable to retrieve Servers and/or Music Libraries")
        conf_libraries.options = tuple(
            # use the same value for both the value and the title
            # until we find out what plex uses as stable identifiers
            ConfigValueOption(
                title=x,
                value=x,
            )
            for x in libraries
        )
        # select first library as (default) value
        conf_libraries.default_value = libraries[0]
        conf_libraries.value = libraries[0]
    # return the collected config entries
    return (
        ConfigEntry(
            key=CONF_AUTH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label="Authentication token for MyPlex.tv",
            description="You need to link Music Assistant to your MyPlex account.",
            action=CONF_ACTION_AUTH,
            action_label="Authenticate on MyPlex.tv",
            value=values.get(CONF_AUTH_TOKEN) if values else None,
        ),
        conf_libraries,
    )


class PlexProvider(MusicProvider):
    """Provider for a plex music library."""

    _plex_server: PlexServer = None
    _plex_library: PlexMusicSection = None

    async def handle_setup(self) -> None:
        """Set up the music provider by connecting to the server."""
        # silence urllib logger
        logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
        server_name, library_name = self.config.get_value(CONF_LIBRARY_ID).split(" / ", 1)

        def connect() -> PlexServer:
            try:
                plex_account = MyPlexAccount(token=self.config.get_value(CONF_AUTH_TOKEN))
            except plexapi.exceptions.BadRequest as err:
                if "Invalid token" in str(err):
                    # token invalid, invaidate the config
                    self.mass.config.remove_provider_config_value(self.instance_id, CONF_AUTH_TOKEN)
                    raise LoginFailed("Authentication failed")
                raise LoginFailed() from err
            return plex_account.resource(server_name).connect(None, 10)

        self._plex_server = await self._run_async(connect)
        self._plex_library = await self._run_async(self._plex_server.library.section, library_name)

    @property
    def supported_features(self) -> tuple[ProviderFeature, ...]:
        """Return a list of supported features."""
        return (
            ProviderFeature.LIBRARY_ARTISTS,
            ProviderFeature.LIBRARY_ALBUMS,
            ProviderFeature.LIBRARY_TRACKS,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.BROWSE,
            ProviderFeature.SEARCH,
            ProviderFeature.ARTIST_ALBUMS,
        )

    @property
    def is_unique(self) -> bool:
        """
        Return True if the (non user related) data in this provider instance is unique.

        For example on a global streaming provider (like Spotify),
        the data on all instances is the same.
        For a file provider each instance has other items.
        Setting this to False will only query one instance of the provider for search and lookups.
        Setting this to True will query all instances of this provider for search and lookups.
        """
        return True

    async def resolve_image(self, path: str) -> str | bytes | AsyncGenerator[bytes, None]:
        """Return the full image URL including the auth token."""
        return self._plex_server.url(path, True)

    async def _run_async(self, call: Callable, *args, **kwargs):
        return await self.mass.create_task(call, *args, **kwargs)

    async def _get_data(self, key, cls=None):
        return await self._run_async(self._plex_library.fetchItem, key, cls)

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type,
            key,
            self.instance_id,
            name,
        )

    async def _get_or_create_artist_by_name(self, artist_name) -> Artist:
        query = (
            "SELECT * FROM artists WHERE name = :name AND provider_mappings = :provider_instance"
        )
        params = {
            "name": f"%{artist_name}%",
            "provider_instance": f"%{self.instance_id}%",
        }
        db_artists = await self.mass.music.artists.get_db_items_by_query(query, params)
        if db_artists:
            return ItemMapping.from_item(db_artists[0])

        artist_id = FAKE_ARTIST_PREFIX + artist_name
        artist = Artist(item_id=artist_id, name=artist_name, provider=self.domain)
        artist.add_provider_mapping(
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
        )
        return artist

    async def _parse(self, plex_media) -> MediaItem | None:
        if plex_media.type == "artist":
            return await self._parse_artist(plex_media)
        elif plex_media.type == "album":
            return await self._parse_album(plex_media)
        elif plex_media.type == "track":
            return await self._parse_track(plex_media)
        elif plex_media.type == "playlist":
            return await self._parse_playlist(plex_media)
        return None

    async def _search_track(self, search_query, limit) -> list[PlexTrack]:
        return await self._run_async(
            self._plex_library.searchTracks, title=search_query, limit=limit
        )

    async def _search_album(self, search_query, limit) -> list[PlexAlbum]:
        return await self._run_async(
            self._plex_library.searchAlbums, title=search_query, limit=limit
        )

    async def _search_artist(self, search_query, limit) -> list[PlexArtist]:
        return await self._run_async(
            self._plex_library.searchArtists, title=search_query, limit=limit
        )

    async def _search_playlist(self, search_query, limit) -> list[PlexPlaylist]:
        return await self._run_async(self._plex_library.playlists, title=search_query, limit=limit)

    async def _search_track_advanced(self, limit, **kwargs) -> list[PlexTrack]:
        return await self._run_async(self._plex_library.searchTracks, filters=kwargs, limit=limit)

    async def _search_album_advanced(self, limit, **kwargs) -> list[PlexAlbum]:
        return await self._run_async(self._plex_library.searchAlbums, filters=kwargs, limit=limit)

    async def _search_artist_advanced(self, limit, **kwargs) -> list[PlexArtist]:
        return await self._run_async(self._plex_library.searchArtists, filters=kwargs, limit=limit)

    async def _search_playlist_advanced(self, limit, **kwargs) -> list[PlexPlaylist]:
        return await self._run_async(self._plex_library.playlists, filters=kwargs, limit=limit)

    async def _search_and_parse(
        self, search_coro: Coroutine, parse_coro: Callable
    ) -> list[MediaItem]:
        task_results = []
        async with TaskGroup() as tg:
            for item in await search_coro:
                task_results.append(tg.create_task(parse_coro(item)))

        results = []
        for task in task_results:
            results.append(task.result())

        return results

    async def _parse_album(self, plex_album: PlexAlbum) -> Album:
        """Parse a Plex Album response to an Album model object."""
        album_id = plex_album.key
        album = Album(
            item_id=album_id,
            provider=self.domain,
            name=plex_album.title,
        )
        if plex_album.year:
            album.year = plex_album.year
        if thumb := plex_album.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            album.metadata.images = [MediaItemImage(ImageType.THUMB, thumb, self.instance_id)]
        if plex_album.summary:
            album.metadata.description = plex_album.summary

        album.artists.append(
            self._get_item_mapping(MediaType.ARTIST, plex_album.parentKey, plex_album.parentTitle)
        )

        album.add_provider_mapping(
            ProviderMapping(
                item_id=str(album_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=plex_album.getWebURL(),
            )
        )
        return album

    async def _parse_artist(self, plex_artist: PlexArtist) -> Artist:
        """Parse a Plex Artist response to Artist model object."""
        artist_id = plex_artist.key
        if not artist_id:
            raise InvalidDataError("Artist does not have a valid ID")
        artist = Artist(item_id=artist_id, name=plex_artist.title, provider=self.domain)
        if plex_artist.summary:
            artist.metadata.description = plex_artist.summary
        if thumb := plex_artist.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            artist.metadata.images = [MediaItemImage(ImageType.THUMB, thumb, self.instance_id)]
        artist.add_provider_mapping(
            ProviderMapping(
                item_id=str(artist_id),
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=plex_artist.getWebURL(),
            )
        )
        return artist

    async def _parse_playlist(self, plex_playlist: PlexPlaylist) -> Playlist:
        """Parse a Plex Playlist response to a Playlist object."""
        playlist = Playlist(
            item_id=plex_playlist.key, provider=self.domain, name=plex_playlist.title
        )
        if plex_playlist.summary:
            playlist.metadata.description = plex_playlist.summary
        if thumb := plex_playlist.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            playlist.metadata.images = [MediaItemImage(ImageType.THUMB, thumb, self.instance_id)]
        playlist.is_editable = True
        playlist.add_provider_mapping(
            ProviderMapping(
                item_id=plex_playlist.key,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                url=plex_playlist.getWebURL(),
            )
        )
        return playlist

    async def _parse_track(self, plex_track: PlexTrack) -> Track:
        """Parse a Plex Track response to a Track model object."""
        track = Track(item_id=plex_track.key, provider=self.instance_id, name=plex_track.title)

        if plex_track.originalTitle and plex_track.originalTitle != plex_track.grandparentTitle:
            # The artist of the track if different from the album's artist.
            # For this kind of artist, we just know the name, so we create a fake artist,
            # if it does not already exist.
            track.artists.append(await self._get_or_create_artist_by_name(plex_track.originalTitle))
        elif plex_track.grandparentKey:
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST, plex_track.grandparentKey, plex_track.grandparentTitle
                )
            )
        else:
            raise InvalidDataError("No artist was found for track")

        if thumb := plex_track.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            track.metadata.images = [MediaItemImage(ImageType.THUMB, thumb, self.instance_id)]
        if plex_track.parentKey:
            track.album = self._get_item_mapping(
                MediaType.ALBUM, plex_track.parentKey, plex_track.parentKey
            )
        if plex_track.duration:
            track.duration = int(plex_track.duration / 1000)
        if plex_track.trackNumber:
            track.track_number = plex_track.trackNumber
        if plex_track.parentIndex:
            track.disc_number = plex_track.parentIndex
        available = False
        content = None

        if plex_track.media:
            available = True
            content = plex_track.media[0].container

        track.add_provider_mapping(
            ProviderMapping(
                item_id=plex_track.key,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                available=available,
                content_type=ContentType.try_parse(content) if content else ContentType.UNKNOWN,
                url=plex_track.getWebURL(),
            )
        )
        return track

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType] | None = None,
        limit: int = 20,
    ) -> SearchResults:
        """Perform search on the plex library.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        if not media_types:
            media_types = [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST]

        tasks = {}

        async with TaskGroup() as tg:
            for media_type in media_types:
                if media_type == MediaType.ARTIST:
                    tasks[MediaType.ARTIST] = tg.create_task(
                        self._search_and_parse(
                            self._search_artist(search_query, limit), self._parse_artist
                        )
                    )
                elif media_type == MediaType.ALBUM:
                    tasks[MediaType.ARTIST] = tg.create_task(
                        self._search_and_parse(
                            self._search_album(search_query, limit), self._parse_album
                        )
                    )
                elif media_type == MediaType.TRACK:
                    tasks[MediaType.ARTIST] = tg.create_task(
                        self._search_and_parse(
                            self._search_track(search_query, limit), self._parse_track
                        )
                    )
                elif media_type == MediaType.PLAYLIST:
                    tasks[MediaType.ARTIST] = tg.create_task(
                        self._search_and_parse(
                            self._search_playlist(search_query, limit), self._parse_playlist
                        )
                    )

        search_results = SearchResults()

        for media_type, task in tasks.items():
            if media_type == MediaType.ARTIST:
                search_results.artists = task.result()
            elif media_type == MediaType.ALBUM:
                search_results.albums = task.result()
            elif media_type == MediaType.TRACK:
                search_results.tracks = task.result()
            elif media_type == MediaType.PLAYLIST:
                search_results.playlists = task.result()

        return search_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Plex Music."""
        artists_obj = await self._run_async(self._plex_library.all)
        for artist in artists_obj:
            yield await self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Plex Music."""
        albums_obj = await self._run_async(self._plex_library.albums)
        for album in albums_obj:
            yield await self._parse_album(album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await self._run_async(self._plex_library.playlists)
        for playlist in playlists_obj:
            yield await self._parse_playlist(playlist)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Plex Music."""
        tracks_obj = await self._search_track(None, limit=99999)
        for track in tracks_obj:
            yield await self._parse_track(track)

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        if plex_album := await self._get_data(prov_album_id, PlexAlbum):
            return await self._parse_album(plex_album)
        raise MediaNotFoundError(f"Item {prov_album_id} not found")

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        plex_album = await self._get_data(prov_album_id, PlexAlbum)

        tracks = []
        for plex_track in await self._run_async(plex_album.tracks):
            track = await self._parse_track(plex_track)
            tracks.append(track)
        return tracks

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            # This artist does not exist in plex, so we can just load it from DB.

            if db_artist := await self.mass.music.artists.get_db_item_by_prov_id(
                prov_artist_id, self.instance_id
            ):
                return db_artist
            raise MediaNotFoundError(f"Artist not found: {prov_artist_id}")

        if plex_artist := await self._get_data(prov_artist_id, PlexArtist):
            return await self._parse_artist(plex_artist)
        raise MediaNotFoundError(f"Item {prov_artist_id} not found")

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        if plex_track := await self._get_data(prov_track_id, PlexTrack):
            return await self._parse_track(plex_track)
        raise MediaNotFoundError(f"Item {prov_track_id} not found")

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        if plex_playlist := await self._get_data(prov_playlist_id, PlexPlaylist):
            return await self._parse_playlist(plex_playlist)
        raise MediaNotFoundError(f"Item {prov_playlist_id} not found")

    async def get_playlist_tracks(  # type: ignore[return]
        self, prov_playlist_id: str
    ) -> AsyncGenerator[Track, None]:
        """Get all playlist tracks for given playlist id."""
        plex_playlist = await self._get_data(prov_playlist_id, PlexPlaylist)

        playlist_items = await self._run_async(plex_playlist.items)

        if not playlist_items:
            yield None
        for index, plex_track in enumerate(playlist_items):
            track = await self._parse_track(plex_track)
            if track:
                track.position = index + 1
                yield track

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of albums for the given artist."""
        if not prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            plex_artist = await self._get_data(prov_artist_id, PlexArtist)
            plex_albums = await self._run_async(plex_artist.albums)
            if plex_albums:
                albums = []
                for album_obj in plex_albums:
                    albums.append(await self._parse_album(album_obj))
                return albums
        return []

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Get streamdetails for a track."""
        plex_track = await self._get_data(item_id, PlexTrack)
        if not plex_track or not plex_track.media:
            raise MediaNotFoundError(f"track {item_id} not found")

        media: PlexMedia = plex_track.media[0]

        media_type = (
            ContentType.try_parse(media.container) if media.container else ContentType.UNKNOWN
        )
        media_part: PlexMediaPart = media.parts[0]
        audio_stream: PlexAudioStream = media_part.audioStreams()[0]

        stream_details = StreamDetails(
            item_id=plex_track.key,
            provider=self.instance_id,
            content_type=media_type,
            duration=plex_track.duration,
            channels=media.audioChannels,
            data=plex_track,
        )

        if audio_stream.loudness:
            stream_details.loudness = audio_stream.loudness

        if media_type != ContentType.M4A:
            stream_details.direct = self._plex_server.url(media_part.key, True)
            if audio_stream.samplingRate:
                stream_details.sample_rate = audio_stream.samplingRate
            if audio_stream.bitDepth:
                stream_details.bit_depth = audio_stream.bitDepth

        else:
            url = plex_track.getStreamURL()
            media_info = await parse_tags(url)

            stream_details.channels = media_info.channels
            stream_details.content_type = ContentType.try_parse(media_info.format)
            stream_details.sample_rate = media_info.sample_rate
            stream_details.bit_depth = media_info.bits_per_sample

        return stream_details

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        url = streamdetails.data.getStreamURL(offset=seek_position)

        timeout = ClientTimeout(total=0, connect=30, sock_read=600)
        async with self.mass.http_session.get(url, timeout=timeout) as resp:
            async for chunk in resp.content.iter_any():
                yield chunk
