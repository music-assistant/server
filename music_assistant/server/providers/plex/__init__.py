"""Plex musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import logging
from asyncio import TaskGroup
from typing import TYPE_CHECKING, Any

import plexapi.exceptions
from aiohttp import ClientTimeout
from plexapi.audio import Album as PlexAlbum
from plexapi.audio import Artist as PlexArtist
from plexapi.audio import Playlist as PlexPlaylist
from plexapi.audio import Track as PlexTrack
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
    AlbumTrack,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItem,
    MediaItemChapter,
    MediaItemImage,
    Playlist,
    PlaylistTrack,
    ProviderMapping,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.server.helpers.auth import AuthenticationHelper
from music_assistant.server.helpers.tags import parse_tags
from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.server.providers.plex.helpers import discover_local_servers, get_libraries

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine

    from plexapi.library import MusicSection as PlexMusicSection
    from plexapi.media import AudioStream as PlexAudioStream
    from plexapi.media import Media as PlexMedia
    from plexapi.media import MediaPart as PlexMediaPart

    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CONF_ACTION_AUTH = "auth"
CONF_ACTION_LIBRARY = "library"
CONF_AUTH_TOKEN = "token"
CONF_LIBRARY_ID = "library_id"
CONF_LOCAL_SERVER_IP = "local_server_ip"
CONF_LOCAL_SERVER_PORT = "local_server_port"
CONF_USE_GDM = "use_gdm"
CONF_ACTION_GDM = "gdm"
FAKE_ARTIST_PREFIX = "_fake://"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if not config.get_value(CONF_AUTH_TOKEN):
        msg = "Invalid login credentials"
        raise LoginFailed(msg)

    prov = PlexProvider(mass, manifest, config)
    await prov.handle_async_init()
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
    conf_gdm = ConfigEntry(
        key=CONF_USE_GDM,
        type=ConfigEntryType.BOOLEAN,
        label="GDM",
        default_value=False,
        description='Enable "GDM" to discover local Plex servers automatically.',
        action=CONF_ACTION_GDM,
        action_label="Use Plex GDM to discover local servers",
    )
    if action == CONF_ACTION_GDM and (server_details := await discover_local_servers()):
        if server_details[0] is None and server_details[1] is None:
            values[CONF_LOCAL_SERVER_IP] = "Discovery failed, please add IP manually"
            values[CONF_LOCAL_SERVER_PORT] = "Discovery failed, please add Port manually"
        else:
            values[CONF_LOCAL_SERVER_IP] = server_details[0]
            values[CONF_LOCAL_SERVER_PORT] = server_details[1]
    # config flow auth action/step (authenticate button clicked)
    if action == CONF_ACTION_AUTH:
        async with AuthenticationHelper(mass, values["session_id"]) as auth_helper:
            plex_auth = MyPlexPinLogin(headers={"X-Plex-Product": "Music Assistant"}, oauth=True)
            auth_url = plex_auth.oauthUrl(auth_helper.callback_url)
            await auth_helper.authenticate(auth_url)
            if not plex_auth.checkLogin():
                msg = "Authentication to MyPlex failed"
                raise LoginFailed(msg)
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
        server_http_ip = values.get(CONF_LOCAL_SERVER_IP)
        server_http_port = values.get(CONF_LOCAL_SERVER_PORT)
        if not (libraries := await get_libraries(mass, token, server_http_ip, server_http_port)):
            msg = "Unable to retrieve Servers and/or Music Libraries"
            raise LoginFailed(msg)
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
        conf_gdm,
        ConfigEntry(
            key=CONF_LOCAL_SERVER_IP,
            type=ConfigEntryType.STRING,
            label="Local server IP",
            description="The local server IP (e.g. 192.168.1.77)",
            required=True,
            value=values.get(CONF_LOCAL_SERVER_IP) if values else None,
        ),
        ConfigEntry(
            key=CONF_LOCAL_SERVER_PORT,
            type=ConfigEntryType.STRING,
            label="Local server port",
            description="The local server port (e.g. 32400)",
            required=True,
            value=values.get(CONF_LOCAL_SERVER_PORT) if values else None,
        ),
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
    _myplex_account: MyPlexAccount = None

    async def handle_async_init(self) -> None:
        """Set up the music provider by connecting to the server."""
        # silence loggers
        logging.getLogger("plexapi").setLevel(self.logger.level + 10)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.INFO)
        _, library_name = self.config.get_value(CONF_LIBRARY_ID).split(" / ", 1)

        def connect() -> PlexServer:
            try:
                plex_server = PlexServer(
                    f"http://{self.config.get_value(CONF_LOCAL_SERVER_IP)}:{self.config.get_value(CONF_LOCAL_SERVER_PORT)}",
                    token=self.config.get_value(CONF_AUTH_TOKEN),
                )
            except plexapi.exceptions.BadRequest as err:
                if "Invalid token" in str(err):
                    # token invalid, invalidate the config
                    self.mass.config.remove_provider_config_value(self.instance_id, CONF_AUTH_TOKEN)
                    msg = "Authentication failed"
                    raise LoginFailed(msg)
                raise LoginFailed from err
            return plex_server

        self._myplex_account = await self.get_myplex_account_and_refresh_token(
            self.config.get_value(CONF_AUTH_TOKEN)
        )
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
    def is_streaming_provider(self) -> bool:
        """
        Return True if the provider is a streaming provider.

        This literally means that the catalog is not the same as the library contents.
        For local based providers (files, plex), the catalog is the same as the library content.
        It also means that data is if this provider is NOT a streaming provider,
        data cross instances is unique, the catalog and library differs per instance.

        Setting this to True will only query one instance of the provider for search and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        return False

    async def resolve_image(self, path: str) -> str | bytes | AsyncGenerator[bytes, None]:
        """Return the full image URL including the auth token."""
        return self._plex_server.url(path, True)

    async def _run_async(self, call: Callable, *args, **kwargs):
        await self.get_myplex_account_and_refresh_token(self.config.get_value(CONF_AUTH_TOKEN))
        return await self.mass.create_task(call, *args, **kwargs)

    async def _get_data(self, key, cls=None):
        return await self._run_async(self._plex_library.fetchItem, key, cls)

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    async def _get_or_create_artist_by_name(self, artist_name) -> Artist:
        query = "WHERE name = :name AND provider_mappings = :provider_instance"
        params = {
            "name": f"%{artist_name}%",
            "provider_instance": f"%{self.instance_id}%",
        }
        paged_list = await self.mass.music.artists.library_items(
            extra_query=query, extra_query_params=params
        )
        if paged_list and paged_list.items:
            return ItemMapping.from_item(paged_list.items[0])

        artist_id = FAKE_ARTIST_PREFIX + artist_name
        return Artist(
            item_id=artist_id,
            name=artist_name,
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

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
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_album.getWebURL(),
                )
            },
        )
        if plex_album.year:
            album.year = plex_album.year
        if thumb := plex_album.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            album.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=thumb, provider=self.instance_id)
            ]
        if plex_album.summary:
            album.metadata.description = plex_album.summary

        album.artists.append(
            self._get_item_mapping(
                MediaType.ARTIST,
                plex_album.parentKey,
                plex_album.parentTitle,
            )
        )
        return album

    async def _parse_artist(self, plex_artist: PlexArtist) -> Artist:
        """Parse a Plex Artist response to Artist model object."""
        artist_id = plex_artist.key
        if not artist_id:
            msg = "Artist does not have a valid ID"
            raise InvalidDataError(msg)
        artist = Artist(
            item_id=artist_id,
            name=plex_artist.title,
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_artist.getWebURL(),
                )
            },
        )
        if plex_artist.summary:
            artist.metadata.description = plex_artist.summary
        if thumb := plex_artist.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            artist.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=thumb, provider=self.instance_id)
            ]
        return artist

    async def _parse_playlist(self, plex_playlist: PlexPlaylist) -> Playlist:
        """Parse a Plex Playlist response to a Playlist object."""
        playlist = Playlist(
            item_id=plex_playlist.key,
            provider=self.domain,
            name=plex_playlist.title,
            provider_mappings={
                ProviderMapping(
                    item_id=plex_playlist.key,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_playlist.getWebURL(),
                )
            },
        )
        if plex_playlist.summary:
            playlist.metadata.description = plex_playlist.summary
        if thumb := plex_playlist.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            playlist.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=thumb, provider=self.instance_id)
            ]
        playlist.is_editable = True
        return playlist

    async def _parse_track(
        self, plex_track: PlexTrack, extra_init_kwargs: dict[str, Any] | None = None
    ) -> Track | AlbumTrack | PlaylistTrack:
        """Parse a Plex Track response to a Track model object."""
        if extra_init_kwargs and "position" in extra_init_kwargs:
            track_class = PlaylistTrack
        elif (
            extra_init_kwargs
            and "disc_number" in extra_init_kwargs
            and "track_number" in extra_init_kwargs
        ):
            track_class = AlbumTrack
        else:
            track_class = Track
        if plex_track.media:
            available = True
            content = plex_track.media[0].container
        else:
            available = False
            content = None
        track = track_class(
            item_id=plex_track.key,
            provider=self.instance_id,
            name=plex_track.title,
            **extra_init_kwargs or {},
            provider_mappings={
                ProviderMapping(
                    item_id=plex_track.key,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=available,
                    audio_format=AudioFormat(
                        content_type=(
                            ContentType.try_parse(content) if content else ContentType.UNKNOWN
                        ),
                    ),
                    url=plex_track.getWebURL(),
                )
            },
        )

        if plex_track.originalTitle and plex_track.originalTitle != plex_track.grandparentTitle:
            # The artist of the track if different from the album's artist.
            # For this kind of artist, we just know the name, so we create a fake artist,
            # if it does not already exist.
            track.artists.append(await self._get_or_create_artist_by_name(plex_track.originalTitle))
        elif plex_track.grandparentKey:
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST,
                    plex_track.grandparentKey,
                    plex_track.grandparentTitle,
                )
            )
        else:
            msg = "No artist was found for track"
            raise InvalidDataError(msg)

        if thumb := plex_track.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            track.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=thumb, provider=self.instance_id)
            ]
        if plex_track.parentKey:
            track.album = self._get_item_mapping(
                MediaType.ALBUM, plex_track.parentKey, plex_track.parentKey
            )
        if plex_track.duration:
            track.duration = int(plex_track.duration / 1000)
        if plex_track.chapters:
            track.metadata.chapters = [
                MediaItemChapter(
                    chapter_id=plex_chapter.id,
                    position_start=plex_chapter.start,
                    position_end=plex_chapter.end,
                    title=plex_chapter.title,
                )
                for plex_chapter in plex_track.chapters
            ]

        available = False
        content = None

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
            media_types = [
                MediaType.ARTIST,
                MediaType.ALBUM,
                MediaType.TRACK,
                MediaType.PLAYLIST,
            ]

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
                            self._search_playlist(search_query, limit),
                            self._parse_playlist,
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
        msg = f"Item {prov_album_id} not found"
        raise MediaNotFoundError(msg)

    async def get_album_tracks(self, prov_album_id: str) -> list[AlbumTrack]:
        """Get album tracks for given album id."""
        plex_album: PlexAlbum = await self._get_data(prov_album_id, PlexAlbum)
        tracks = []
        for idx, plex_track in enumerate(await self._run_async(plex_album.tracks), 1):
            track = await self._parse_track(
                plex_track,
                {
                    "disc_number": plex_track.parentIndex,
                    "track_number": plex_track.trackNumber or idx,
                },
            )
            tracks.append(track)
        return tracks

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            # This artist does not exist in plex, so we can just load it from DB.

            if db_artist := await self.mass.music.artists.get_library_item_by_prov_id(
                prov_artist_id, self.instance_id
            ):
                return db_artist
            msg = f"Artist not found: {prov_artist_id}"
            raise MediaNotFoundError(msg)

        if plex_artist := await self._get_data(prov_artist_id, PlexArtist):
            return await self._parse_artist(plex_artist)
        msg = f"Item {prov_artist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        if plex_track := await self._get_data(prov_track_id, PlexTrack):
            return await self._parse_track(plex_track)
        msg = f"Item {prov_track_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        if plex_playlist := await self._get_data(prov_playlist_id, PlexPlaylist):
            return await self._parse_playlist(plex_playlist)
        msg = f"Item {prov_playlist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist_tracks(  # type: ignore[return]
        self, prov_playlist_id: str
    ) -> AsyncGenerator[Track, None]:
        """Get all playlist tracks for given playlist id."""
        plex_playlist: PlexPlaylist = await self._get_data(prov_playlist_id, PlexPlaylist)
        playlist_items = await self._run_async(plex_playlist.items)

        for index, plex_track in enumerate(playlist_items or []):
            if track := await self._parse_track(plex_track, {"position": index + 1}):
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
            msg = f"track {item_id} not found"
            raise MediaNotFoundError(msg)

        media: PlexMedia = plex_track.media[0]

        media_type = (
            ContentType.try_parse(media.container) if media.container else ContentType.UNKNOWN
        )
        media_part: PlexMediaPart = media.parts[0]
        audio_stream: PlexAudioStream = media_part.audioStreams()[0]

        stream_details = StreamDetails(
            item_id=plex_track.key,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=media_type,
                channels=media.audioChannels,
            ),
            duration=plex_track.duration,
            data=plex_track,
        )

        if audio_stream.loudness:
            stream_details.loudness = audio_stream.loudness

        if media_type != ContentType.M4A:
            stream_details.direct = self._plex_server.url(media_part.key, True)
            if audio_stream.samplingRate:
                stream_details.audio_format.sample_rate = audio_stream.samplingRate
            if audio_stream.bitDepth:
                stream_details.audio_format.bit_depth = audio_stream.bitDepth

        else:
            url = plex_track.getStreamURL()
            media_info = await parse_tags(url)

            stream_details.audio_format.channels = media_info.channels
            stream_details.audio_format.content_type = ContentType.try_parse(media_info.format)
            stream_details.audio_format.sample_rate = media_info.sample_rate
            stream_details.audio_format.bit_depth = media_info.bits_per_sample

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

    async def get_myplex_account_and_refresh_token(self, auth_token: str) -> MyPlexAccount:
        """Get a MyPlexAccount object and refresh the token if needed."""

        def _refresh_plex_token():
            if self._myplex_account is None:
                myplex_account = MyPlexAccount(token=auth_token)
                self._myplex_account = myplex_account
            self._myplex_account.ping()
            return self._myplex_account

        return await asyncio.to_thread(_refresh_plex_token)
