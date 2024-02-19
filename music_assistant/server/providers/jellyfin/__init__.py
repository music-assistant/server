"""Jellyfin support for MusicAssistant."""

from __future__ import annotations

import logging
import mimetypes
import socket
import uuid
from asyncio import TaskGroup
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine

from aiohttp import ClientTimeout
from jellyfin_apiclient_python import JellyfinClient
from jellyfin_apiclient_python.api import API

from music_assistant.common.models.config_entries import (
    ConfigEntry,
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
from music_assistant.common.models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
)
from music_assistant.common.models.media_items import (
    Album,
    AlbumTrack,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItem,
    MediaItemImage,
    Playlist,
    PlaylistTrack,
    ProviderMapping,
    SearchResults,
    StreamDetails,
    Track,
)
from music_assistant.common.models.media_items import Album as JellyfinAlbum
from music_assistant.common.models.media_items import Artist as JellyfinArtist
from music_assistant.common.models.media_items import Playlist as JellyfinPlaylist
from music_assistant.common.models.media_items import Track as JellyfinTrack

if TYPE_CHECKING:
    from music_assistant.common.models.provider import ProviderManifest

from music_assistant.constants import VARIOUS_ARTISTS_NAME

if TYPE_CHECKING:
    from music_assistant.server import MusicAssistant
if TYPE_CHECKING:
    from music_assistant.server.models import ProviderInstanceType

from music_assistant.server.models.music_provider import MusicProvider

from .const import (
    CLIENT_VERSION,
    ITEM_KEY_ALBUM,
    ITEM_KEY_ALBUM_ARTIST,
    ITEM_KEY_ARTIST_ITEMS,
    ITEM_KEY_CAN_DOWNLOAD,
    ITEM_KEY_COLLECTION_TYPE,
    ITEM_KEY_ID,
    ITEM_KEY_IMAGE_TAGS,
    ITEM_KEY_INDEX_NUMBER,
    ITEM_KEY_MEDIA_CHANNELS,
    ITEM_KEY_MEDIA_CODEC,
    ITEM_KEY_MEDIA_SOURCES,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_MUSICBRAINZ_ARTIST,
    ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP,
    ITEM_KEY_MUSICBRAINZ_TRACK,
    ITEM_KEY_NAME,
    ITEM_KEY_OVERVIEW,
    ITEM_KEY_PARENT_ID,
    ITEM_KEY_PARENT_INDEX_NUM,
    ITEM_KEY_PRODUCTION_YEAR,
    ITEM_KEY_PROVIDER_IDS,
    ITEM_KEY_RUNTIME_TICKS,
    ITEM_KEY_SORT_NAME,
    ITEM_TYPE_ALBUM,
    ITEM_TYPE_ARTIST,
    ITEM_TYPE_AUDIO,
    MAX_IMAGE_WIDTH,
    USER_APP_NAME,
)

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
FAKE_ARTIST_PREFIX = "_fake://"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = JellyfinProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # pylint: disable=W0613
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
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_URL,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The url of the Jellyfin server to connect to.",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
            description="The username to authenticate to the remote server."
            "the remote host, For example 'media'.",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
            description="The password to authenticate to the remote server.",
        ),
    )


class JellyfinProvider(MusicProvider):
    """Provider for a jellyfin music library."""

    # _jellyfin_server : JellyfinClient = None

    async def handle_async_init(self) -> None:
        """Initialize provider(instance) with given configuration."""
        logging.getLogger("pytube").setLevel(self.logger.level + 10)
        logging.getLogger("ytmusicapi").setLevel(self.logger.level + 10)

        def connect() -> JellyfinClient:
            try:
                client = JellyfinClient()
                device_name = socket.gethostname()
                device_id = str(uuid.uuid4())
                client.config.app(USER_APP_NAME, CLIENT_VERSION, device_name, device_id)
                if CONF_URL.startswith("https://"):
                    JellyfinClient.config.data["auth.ssl"] = True
                else:
                    client.config.data["auth.ssl"] = False
                jellyfin_server_url = self.config.get_value(CONF_URL)
                jellyfin_server_user = self.config.get_value(CONF_USERNAME)
                jellyfin_server_password = self.config.get_value(CONF_PASSWORD)
                client.auth.connect_to_address(jellyfin_server_url)
                client.auth.login(
                    jellyfin_server_url, jellyfin_server_user, jellyfin_server_password
                )
                credentials = client.auth.credentials.get_credentials()
                server = credentials["Servers"][0]
                server["username"] = jellyfin_server_user
                _jellyfin_server = client
                # json.dumps(server)
            except MusicAssistantError as err:
                msg = "Authentication failed: %s", str(err)
                raise LoginFailed(msg)
            return _jellyfin_server

        self._jellyfin_server = await self._run_async(connect)

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

    async def _run_async(self, call: Callable, *args, **kwargs):
        return await self.mass.create_task(call, *args, **kwargs)

    async def resolve_image(self, path: str) -> str | bytes | AsyncGenerator[bytes, None]:
        """Return the full image URL including the auth token."""
        return path

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )

    async def _parse(self, jellyfin_media) -> MediaItem | None:
        if jellyfin_media.type == "artist":
            return await self._parse_artist(jellyfin_media)
        elif jellyfin_media.type == "album":
            return await self._parse_album(jellyfin_media)
        elif jellyfin_media.type == "track":
            return await self._parse_track(jellyfin_media)
        elif jellyfin_media.type == "playlist":
            return await self._parse_playlist(jellyfin_media)
        return None

    async def _search_track(self, search_query, limit) -> list[JellyfinTrack]:
        resultset = await self._run_async(
            API.search_media_items,
            self._jellyfin_server.jellyfin,
            term=search_query,
            media=ITEM_TYPE_AUDIO,
            limit=limit,
        )
        return resultset["Items"]

    async def _search_album(self, search_query, limit) -> list[JellyfinAlbum]:
        if "-" in search_query:
            searchterms = search_query.split(" - ")
            albumname = searchterms[1]
        else:
            albumname = search_query
        resultset = await self._run_async(
            API.search_media_items,
            self._jellyfin_server.jellyfin,
            term=albumname,
            media=ITEM_TYPE_ALBUM,
            limit=limit,
        )
        return resultset["Items"]

    async def _search_artist(self, search_query, limit) -> list[JellyfinArtist]:
        resultset = await self._run_async(
            API.search_media_items,
            self._jellyfin_server.jellyfin,
            term=search_query,
            media=ITEM_TYPE_ARTIST,
            limit=limit,
        )
        return resultset["Items"]

    async def _search_playlist(self, search_query, limit) -> list[JellyfinPlaylist]:
        resultset = await self._run_async(
            API.search_media_items,
            self._jellyfin_server.jellyfin,
            term=search_query,
            media="Playlist",
            limit=limit,
        )
        return resultset["Items"]

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

    async def _parse_album(self, jellyfin_album: dict[str, Any]) -> Album:
        """Parse a Jellyfin Album response to an Album model object."""
        album_id = jellyfin_album[ITEM_KEY_ID]
        album = Album(
            item_id=album_id,
            provider=self.domain,
            name=jellyfin_album[ITEM_KEY_NAME],
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        current_jellyfin_album = API.get_item(self._jellyfin_server.jellyfin, album_id)
        if ITEM_KEY_PRODUCTION_YEAR in current_jellyfin_album:
            album.year = current_jellyfin_album[ITEM_KEY_PRODUCTION_YEAR]
        if thumb := self._get_thumbnail_url(self._jellyfin_server, jellyfin_album):
            album.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=thumb, provider=self.instance_id)
            ]
        if ITEM_KEY_OVERVIEW in current_jellyfin_album:
            album.metadata.description = current_jellyfin_album[ITEM_KEY_OVERVIEW]
        if ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP in current_jellyfin_album[ITEM_KEY_PROVIDER_IDS]:
            musicbrainzid = current_jellyfin_album[ITEM_KEY_PROVIDER_IDS][
                ITEM_KEY_MUSICBRAINZ_RELEASE_GROUP
            ]
            if len(musicbrainzid.split("-")) == 5:
                album.mbid = musicbrainzid
        if ITEM_KEY_SORT_NAME in current_jellyfin_album:
            album.sort_name = current_jellyfin_album[ITEM_KEY_SORT_NAME]
        if ITEM_KEY_ALBUM_ARTIST in current_jellyfin_album:
            album.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST,
                    current_jellyfin_album[ITEM_KEY_PARENT_ID],
                    current_jellyfin_album[ITEM_KEY_ALBUM_ARTIST],
                )
            )
        elif len(current_jellyfin_album[ITEM_KEY_ARTIST_ITEMS]) >= 1:
            num_artists = len(current_jellyfin_album[ITEM_KEY_ARTIST_ITEMS])
            for i in range(num_artists):
                album.artists.append(
                    self._get_item_mapping(
                        MediaType.ARTIST,
                        current_jellyfin_album[ITEM_KEY_ARTIST_ITEMS][i][ITEM_KEY_ID],
                        current_jellyfin_album[ITEM_KEY_ARTIST_ITEMS][i][ITEM_KEY_NAME],
                    )
                )
        return album

    async def _parse_artist(self, jellyfin_artist: dict[str, Any]) -> Artist:
        """Parse a Jellyfin Artist response to Artist model object."""
        artist_id = jellyfin_artist[ITEM_KEY_ID]
        current_artist = API.get_item(self._jellyfin_server.jellyfin, artist_id)
        if not artist_id:
            msg = "Artist does not have a valid ID"
            raise InvalidDataError(msg)
        artist = Artist(
            item_id=artist_id,
            name=jellyfin_artist[ITEM_KEY_NAME],
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if ITEM_KEY_OVERVIEW in current_artist:
            artist.metadata.description = current_artist[ITEM_KEY_OVERVIEW]
        if ITEM_KEY_MUSICBRAINZ_ARTIST in current_artist[ITEM_KEY_PROVIDER_IDS]:
            artist.mbid = current_artist[ITEM_KEY_PROVIDER_IDS][ITEM_KEY_MUSICBRAINZ_ARTIST]
        if ITEM_KEY_SORT_NAME in current_artist:
            artist.sort_name = current_artist[ITEM_KEY_SORT_NAME]
        if thumb := self._get_thumbnail_url(self._jellyfin_server, jellyfin_artist):
            artist.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=thumb, provider=self.instance_id)
            ]
        return artist

    async def _parse_track(
        self, jellyfin_track: dict[str, Any], extra_init_kwargs: dict[str, Any] | None = None
    ) -> Track | AlbumTrack | PlaylistTrack:
        """Parse a Jellyfin Track response to a Track model object."""
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
        current_jellyfin_track = API.get_item(
            self._jellyfin_server.jellyfin, jellyfin_track[ITEM_KEY_ID]
        )
        available = False
        content = None
        available = current_jellyfin_track[ITEM_KEY_CAN_DOWNLOAD]
        content = current_jellyfin_track[ITEM_KEY_MEDIA_STREAMS][0][ITEM_KEY_MEDIA_CODEC]
        track = track_class(
            item_id=jellyfin_track[ITEM_KEY_ID],
            provider=self.instance_id,
            name=jellyfin_track[ITEM_KEY_NAME],
            **extra_init_kwargs or {},
            provider_mappings={
                ProviderMapping(
                    item_id=jellyfin_track[ITEM_KEY_ID],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=available,
                    audio_format=AudioFormat(
                        content_type=(
                            ContentType.try_parse(content) if content else ContentType.UNKNOWN
                        ),
                    ),
                    url=self._get_stream_url(self._jellyfin_server, jellyfin_track[ITEM_KEY_ID]),
                )
            },
        )

        if thumb := self._get_thumbnail_url(self._jellyfin_server, jellyfin_track):
            track.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=thumb, provider=self.instance_id)
            ]
        if len(current_jellyfin_track[ITEM_KEY_ARTIST_ITEMS]) >= 1:
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST,
                    current_jellyfin_track[ITEM_KEY_ARTIST_ITEMS][0][ITEM_KEY_ID],
                    current_jellyfin_track[ITEM_KEY_ARTIST_ITEMS][0][ITEM_KEY_NAME],
                )
            )
            num_artists = len(current_jellyfin_track[ITEM_KEY_ARTIST_ITEMS])
            for i in range(num_artists):
                track.artists.append(
                    self._get_item_mapping(
                        MediaType.ARTIST,
                        current_jellyfin_track[ITEM_KEY_ARTIST_ITEMS][i][ITEM_KEY_ID],
                        current_jellyfin_track[ITEM_KEY_ARTIST_ITEMS][i][ITEM_KEY_NAME],
                    )
                )
        elif ITEM_KEY_PARENT_ID in current_jellyfin_track:
            parent_album = API.get_item(
                self._jellyfin_server.jellyfin, current_jellyfin_track[ITEM_KEY_PARENT_ID]
            )
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST,
                    parent_album[ITEM_KEY_PARENT_ID],
                    parent_album[ITEM_KEY_ALBUM_ARTIST],
                )
            )
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST,
                    parent_album[ITEM_KEY_PARENT_ID],
                    parent_album[ITEM_KEY_ALBUM_ARTIST],
                )
            )
        else:
            track.artists.append(await self._parse_artist(name=VARIOUS_ARTISTS_NAME))
        if ITEM_KEY_PARENT_ID in current_jellyfin_track:
            track.album = self._get_item_mapping(
                MediaType.ALBUM,
                current_jellyfin_track[ITEM_KEY_PARENT_ID],
                current_jellyfin_track[ITEM_KEY_ALBUM],
            )
        if ITEM_KEY_PARENT_INDEX_NUM in current_jellyfin_track:
            track.disc_number = current_jellyfin_track[ITEM_KEY_PARENT_INDEX_NUM]
        if ITEM_KEY_RUNTIME_TICKS in current_jellyfin_track:
            track.duration = int(
                current_jellyfin_track[ITEM_KEY_RUNTIME_TICKS] / 10000000
            )  # 10000000 ticks per millisecond
        track.track_number = current_jellyfin_track.get(ITEM_KEY_INDEX_NUMBER, 99)
        if ITEM_KEY_MUSICBRAINZ_TRACK in current_jellyfin_track[ITEM_KEY_PROVIDER_IDS]:
            track.mbid = current_jellyfin_track[ITEM_KEY_PROVIDER_IDS][ITEM_KEY_MUSICBRAINZ_TRACK]
        return track

    async def _parse_playlist(self, jellyfin_playlist: JellyfinPlaylist) -> Playlist:
        """Parse a Jellyfin Playlist response to a Playlist object."""
        playlistid = jellyfin_playlist[ITEM_KEY_ID]
        playlist = Playlist(
            item_id=playlistid,
            provider=self.domain,
            name=jellyfin_playlist[ITEM_KEY_NAME],
            provider_mappings={
                ProviderMapping(
                    item_id=playlistid,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if ITEM_KEY_OVERVIEW in jellyfin_playlist:
            playlist.metadata.description = jellyfin_playlist[ITEM_KEY_OVERVIEW]
        if thumb := self._get_thumbnail_url(self._jellyfin_server, jellyfin_playlist):
            playlist.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=thumb, provider=self.instance_id)
            ]
        playlist.is_editable = False
        return playlist

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
                    tasks[MediaType.ALBUM] = tg.create_task(
                        self._search_and_parse(
                            self._search_album(search_query, limit), self._parse_album
                        )
                    )
                elif media_type == MediaType.TRACK:
                    tasks[MediaType.TRACK] = tg.create_task(
                        self._search_and_parse(
                            self._search_track(search_query, limit), self._parse_track
                        )
                    )
                elif media_type == MediaType.PLAYLIST:
                    tasks[MediaType.PLAYLIST] = tg.create_task(
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
        """Retrieve all library artists from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries(self._jellyfin_server)
        for jellyfin_library in jellyfin_libraries:
            artists_obj = await self._get_children(
                self._jellyfin_server, jellyfin_library[ITEM_KEY_ID], ITEM_TYPE_ARTIST
            )
            for artist in artists_obj:
                yield await self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries(self._jellyfin_server)
        for jellyfin_library in jellyfin_libraries:
            artists_obj = await self._get_children(
                self._jellyfin_server, jellyfin_library[ITEM_KEY_ID], ITEM_TYPE_ARTIST
            )
            for artist in artists_obj:
                albums_obj = await self._get_children(
                    self._jellyfin_server, artist[ITEM_KEY_ID], ITEM_TYPE_ALBUM
                )
                for album in albums_obj:
                    yield await self._parse_album(album)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries(self._jellyfin_server)
        self._jellyfin_server.default_timeout = 120
        for jellyfin_library in jellyfin_libraries:
            artists_obj = await self._get_children(
                self._jellyfin_server, jellyfin_library[ITEM_KEY_ID], ITEM_TYPE_ARTIST
            )
            for artist in artists_obj:
                albums_obj = await self._get_children(
                    self._jellyfin_server, artist[ITEM_KEY_ID], ITEM_TYPE_ALBUM
                )
                for album in albums_obj:
                    tracks_obj = await self._get_children(
                        self._jellyfin_server, album[ITEM_KEY_ID], ITEM_TYPE_AUDIO
                    )
                    for track in tracks_obj:
                        yield await self._parse_track(track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlist_libraries = await self._get_playlists(self._jellyfin_server)
        for playlist_library in playlist_libraries:
            playlists_obj = await self._get_children(
                self._jellyfin_server, playlist_library[ITEM_KEY_ID], "Playlist"
            )
            for playlist in playlists_obj:
                if playlist["MediaType"] == "Audio":
                    yield await self._parse_playlist(playlist)

    async def get_album(self, prov_album_id) -> Album:
        """Get full album details by id."""
        if jellyfin_album := API.get_item(self._jellyfin_server.jellyfin, prov_album_id):
            return await self._run_async(self._parse_album(jellyfin_album))
        msg = f"Item {prov_album_id} not found"
        raise MediaNotFoundError(msg)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        jellyfin_album_tracks = await self._get_children(
            self._jellyfin_server, prov_album_id, ITEM_TYPE_AUDIO
        )
        tracks = []
        for jellyfin_album_track in jellyfin_album_tracks:
            discnum = jellyfin_album_track.get(ITEM_KEY_PARENT_INDEX_NUM, 1)
            if "IndexNumber" in jellyfin_album_track:
                if jellyfin_album_track["IndexNumber"] >= 1:
                    tracknum = jellyfin_album_track["IndexNumber"]
                else:
                    tracknum = jellyfin_album_track["IndexNumber"]
            else:
                tracknum = 99
            track = await self._parse_track(
                jellyfin_album_track,
                {
                    "disc_number": discnum,
                    "track_number": tracknum,
                },
            )
            tracks.append(track)
        return tracks

    async def get_artist(self, prov_artist_id) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            # This artist does not exist in jellyfin, so we can just load it from DB.

            if db_artist := await self.mass.music.artists.get_db_item_by_prov_id(
                prov_artist_id, self.instance_id
            ):
                return db_artist
            msg = f"Artist not found: {prov_artist_id}"
            raise MediaNotFoundError(msg)

        if jellyfin_artist := API.get_item(self._jellyfin_server.jellyfin, prov_artist_id):
            return await self._parse_artist(jellyfin_artist)
        msg = f"Item {prov_artist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_track(self, prov_track_id) -> Track:
        """Get full track details by id."""
        if jellyfin_track := API.get_item(self._jellyfin_server.jellyfin, prov_track_id):
            return await self._parse_track(jellyfin_track)
        msg = f"Item {prov_track_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        """Get full playlist details by id."""
        if jellyfin_playlist := API.get_item(self._jellyfin_server.jellyfin, prov_playlist_id):
            return await self._parse_playlist(jellyfin_playlist)
        msg = f"Item {prov_playlist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist_tracks(  # type: ignore[return]
        self, prov_playlist_id: str
    ) -> AsyncGenerator[Track, None]:
        """Get all playlist tracks for given playlist id."""
        jellyfin_playlist = API.get_item(self._jellyfin_server.jellyfin, prov_playlist_id)

        playlist_items = await self._get_children(
            self._jellyfin_server, jellyfin_playlist[ITEM_KEY_ID], ITEM_TYPE_AUDIO
        )

        if not playlist_items:
            yield None
        for index, jellyfin_track in enumerate(playlist_items):
            if track := await self._parse_track(jellyfin_track, {"position": index + 1}):
                yield track

    async def get_artist_albums(self, prov_artist_id) -> list[Album]:
        """Get a list of albums for the given artist."""
        if not prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            artists_obj = await self._get_children(
                self._jellyfin_server, prov_artist_id, ITEM_TYPE_ARTIST
            )
            for artist in artists_obj:
                jellyfin_albums = await self._get_children(
                    self._jellyfin_server, artist[ITEM_KEY_ID], ITEM_TYPE_ALBUM
                )
                if jellyfin_albums:
                    albums = []
                    for album_obj in jellyfin_albums:
                        albums.append(await self._parse_album(album_obj))
                    return albums
        return []

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        jellyfin_track = API.get_item(self._jellyfin_server.jellyfin, item_id)
        mimetype = self._media_mime_type(jellyfin_track)
        media_stream = jellyfin_track[ITEM_KEY_MEDIA_STREAMS][0]
        if ITEM_KEY_MEDIA_CODEC in media_stream:
            media_type = ContentType.try_parse(media_stream[ITEM_KEY_MEDIA_CODEC])
        else:
            media_type = ContentType.try_parse(mimetype)
        return StreamDetails(
            item_id=jellyfin_track[ITEM_KEY_ID],
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=media_type,
                channels=jellyfin_track[ITEM_KEY_MEDIA_STREAMS][0][ITEM_KEY_MEDIA_CHANNELS],
            ),
            duration=int(
                jellyfin_track[ITEM_KEY_RUNTIME_TICKS] / 10000000
            ),  # 10000000 ticks per millisecond)
            data=jellyfin_track,
        )

    def _get_thumbnail_url(self, client: JellyfinClient, media_item: dict[str, Any]) -> str | None:
        """Return the URL for the primary image of a media item if available."""
        image_tags = media_item[ITEM_KEY_IMAGE_TAGS]

        if "Primary" not in image_tags:
            return None

        item_id = media_item[ITEM_KEY_ID]
        return API.artwork(client.jellyfin, item_id, "Primary", MAX_IMAGE_WIDTH)

    def _get_stream_url(self, client: JellyfinClient, media_item: str) -> str:
        """Return the stream URL for a media item."""
        return API.audio_url(client.jellyfin, media_item)  # type: ignore[no-any-return]

    async def _get_children(
        self, client: JellyfinClient, parent_id: str, item_type: str
    ) -> list[dict[str, Any]]:
        """Return all children for the parent_id whose item type is item_type."""
        params = {
            "Recursive": "true",
            ITEM_KEY_PARENT_ID: parent_id,
            "IncludeItemTypes": item_type,
        }
        if item_type in ITEM_TYPE_AUDIO:
            params["Fields"] = ITEM_KEY_MEDIA_SOURCES

        result = client.jellyfin.user_items("", params)
        return result["Items"]

    async def _get_music_libraries(self, client: JellyfinClient) -> list[dict[str, Any]]:
        """Return all supported libraries a user has access to."""
        response = API.get_media_folders(client.jellyfin)
        libraries = response["Items"]
        result = []
        for library in libraries:
            if ITEM_KEY_COLLECTION_TYPE in library and library[ITEM_KEY_COLLECTION_TYPE] in "music":
                result.append(library)
        return result

    async def _get_playlists(self, client: JellyfinClient) -> list[dict[str, Any]]:
        """Return all supported libraries a user has access to."""
        response = API.get_media_folders(client.jellyfin)
        libraries = response["Items"]
        result = []
        for library in libraries:
            if (
                ITEM_KEY_COLLECTION_TYPE in library
                and library[ITEM_KEY_COLLECTION_TYPE] in "playlists"
            ):
                result.append(library)
        return result

    def _media_mime_type(self, media_item: dict[str, Any]) -> str | None:
        """Return the mime type of a media item."""
        if not media_item.get(ITEM_KEY_MEDIA_SOURCES):
            return None

        media_source = media_item[ITEM_KEY_MEDIA_SOURCES][0]

        if "Path" not in media_source:
            return None

        path = media_source["Path"]
        mime_type, _ = mimetypes.guess_type(path)

        return mime_type

    async def get_audio_stream(self, streamdetails: StreamDetails) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        url = API.audio_url(self._jellyfin_server.jellyfin, streamdetails.item_id)

        timeout = ClientTimeout(total=0, connect=30, sock_read=600)
        async with self.mass.http_session.get(url, timeout=timeout) as resp:
            async for chunk in resp.content.iter_any():
                yield chunk
