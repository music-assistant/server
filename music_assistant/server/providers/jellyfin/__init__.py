"""Jellyfin support for MusicAssistant."""

from __future__ import annotations

import mimetypes
import socket
import uuid
from asyncio import TaskGroup
from collections.abc import AsyncGenerator

from aiojellyfin import MediaLibrary as JellyMediaLibrary
from aiojellyfin import SessionConfiguration, authenticate_by_name
from aiojellyfin import Track as JellyTrack

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant.common.models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant.common.models.errors import LoginFailed, MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    AudioFormat,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant.common.models.provider import ProviderManifest
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.constants import UNKNOWN_ARTIST_ID_MBID
from music_assistant.server.models import ProviderInstanceType
from music_assistant.server.models.music_provider import MusicProvider
from music_assistant.server.providers.jellyfin.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)
from music_assistant.server.server import MusicAssistant

from .const import (
    ALBUM_FIELDS,
    ARTIST_FIELDS,
    CLIENT_VERSION,
    ITEM_KEY_COLLECTION_TYPE,
    ITEM_KEY_ID,
    ITEM_KEY_MEDIA_CHANNELS,
    ITEM_KEY_MEDIA_CODEC,
    ITEM_KEY_MEDIA_SOURCES,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_NAME,
    ITEM_KEY_RUNTIME_TICKS,
    SUPPORTED_CONTAINER_FORMATS,
    TRACK_FIELDS,
    UNKNOWN_ARTIST_MAPPING,
    USER_APP_NAME,
)

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
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
            required=False,
            description="The password to authenticate to the remote server.",
        ),
        ConfigEntry(
            key=CONF_VERIFY_SSL,
            type=ConfigEntryType.BOOLEAN,
            label="Verify SSL",
            required=False,
            description="Whether or not to verify the certificate of SSL/TLS connections.",
            category="advanced",
            default_value=True,
        ),
    )


class JellyfinProvider(MusicProvider):
    """Provider for a jellyfin music library."""

    async def handle_async_init(self) -> None:
        """Initialize provider(instance) with given configuration."""
        session_config = SessionConfiguration(
            session=self.mass.http_session,
            url=str(self.config.get_value(CONF_URL)),
            verify_ssl=bool(self.config.get_value(CONF_VERIFY_SSL)),
            app_name=USER_APP_NAME,
            app_version=CLIENT_VERSION,
            device_name=socket.gethostname(),
            device_id=str(uuid.uuid4()),
        )

        try:
            self._client = await authenticate_by_name(
                session_config,
                str(self.config.get_value(CONF_USERNAME)),
                str(self.config.get_value(CONF_PASSWORD)),
            )
        except Exception as err:
            raise LoginFailed(f"Authentication failed: {err}") from err

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
        """Return True if the provider is a streaming provider."""
        return False

    async def _search_track(self, search_query: str, limit: int) -> list[Track]:
        resultset = await self._client.tracks(
            search_term=search_query,
            limit=limit,
            enable_user_data=True,
            fields=TRACK_FIELDS,
        )
        tracks = []
        for item in resultset["Items"]:
            tracks.append(parse_track(self.logger, self.instance_id, self._client, item))
        return tracks

    async def _search_album(self, search_query: str, limit: int) -> list[Album]:
        if "-" in search_query:
            searchterms = search_query.split(" - ")
            albumname = searchterms[1]
        else:
            albumname = search_query
        resultset = await self._client.albums(
            search_term=albumname,
            limit=limit,
            enable_user_data=True,
            fields=ALBUM_FIELDS,
        )
        albums = []
        for item in resultset["Items"]:
            albums.append(parse_album(self.logger, self.instance_id, self._client, item))
        return albums

    async def _search_artist(self, search_query: str, limit: int) -> list[Artist]:
        resultset = await self._client.artists(
            search_term=search_query,
            limit=limit,
            enable_user_data=True,
            fields=ARTIST_FIELDS,
        )
        artists = []
        for item in resultset["Items"]:
            artists.append(parse_artist(self.logger, self.instance_id, self._client, item))
        return artists

    async def _search_playlist(self, search_query: str, limit: int) -> list[Playlist]:
        resultset = await self._client.playlists(
            search_term=search_query,
            limit=limit,
            enable_user_data=True,
        )
        playlists = []
        for item in resultset["Items"]:
            playlists.append(parse_playlist(self.instance_id, self._client, item))
        return playlists

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """Perform search on the plex library.

        :param search_query: Search query.
        :param media_types: A list of media_types to include. All types if None.
        :param limit: Number of items to return in the search (per type).
        """
        artists = None
        albums = None
        tracks = None
        playlists = None

        async with TaskGroup() as tg:
            if MediaType.ARTIST in media_types:
                artists = tg.create_task(self._search_artist(search_query, limit))
            if MediaType.ALBUM in media_types:
                albums = tg.create_task(self._search_album(search_query, limit))
            if MediaType.TRACK in media_types:
                tracks = tg.create_task(self._search_track(search_query, limit))
            if MediaType.PLAYLIST in media_types:
                playlists = tg.create_task(self._search_playlist(search_query, limit))

        search_results = SearchResults()

        if artists:
            search_results.artists += artists.result()
        if albums:
            search_results.albums += albums.result()
        if tracks:
            search_results.tracks += tracks.result()
        if playlists:
            search_results.playlists += playlists.result()

        return search_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            offset = 0
            limit = 100

            response = await self._client.artists(
                jellyfin_library[ITEM_KEY_ID],
                start_index=offset,
                limit=limit,
                enable_user_data=True,
                fields=ARTIST_FIELDS,
            )
            for artist in response["Items"]:
                yield parse_artist(self.logger, self.instance_id, self._client, artist)

            while offset < response["TotalRecordCount"]:
                response = await self._client.artists(
                    jellyfin_library[ITEM_KEY_ID],
                    start_index=offset,
                    limit=limit,
                    enable_user_data=True,
                    fields=ARTIST_FIELDS,
                )
                for artist in response["Items"]:
                    yield parse_artist(self.logger, self.instance_id, self._client, artist)

                offset += limit

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            offset = 0
            limit = 100

            response = await self._client.albums(
                jellyfin_library[ITEM_KEY_ID],
                start_index=offset,
                limit=limit,
                enable_user_data=True,
                fields=ALBUM_FIELDS,
            )
            for artist in response["Items"]:
                yield parse_album(self.logger, self.instance_id, self._client, artist)

            while offset < response["TotalRecordCount"]:
                response = await self._client.albums(
                    jellyfin_library[ITEM_KEY_ID],
                    start_index=offset,
                    limit=limit,
                    enable_user_data=True,
                    fields=ALBUM_FIELDS,
                )
                for artist in response["Items"]:
                    yield parse_album(self.logger, self.instance_id, self._client, artist)

                offset += limit

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            offset = 0
            limit = 100

            response = await self._client.tracks(
                jellyfin_library[ITEM_KEY_ID],
                start_index=offset,
                limit=limit,
                enable_user_data=True,
                fields=TRACK_FIELDS,
            )
            for track in response["Items"]:
                yield parse_track(self.logger, self.instance_id, self._client, track)

            while offset < response["TotalRecordCount"]:
                response = await self._client.tracks(
                    jellyfin_library[ITEM_KEY_ID],
                    start_index=offset,
                    limit=limit,
                    enable_user_data=True,
                    fields=TRACK_FIELDS,
                )
                for track in response["Items"]:
                    yield parse_track(self.logger, self.instance_id, self._client, track)

                offset += limit

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlist_libraries = await self._get_playlists()
        for playlist_library in playlist_libraries:
            playlists_obj = await self._client.playlists(playlist_library[ITEM_KEY_ID])
            for playlist in playlists_obj["Items"]:
                if "MediaType" in playlist:  # Only jellyfin has this property
                    if playlist["MediaType"] == "Audio":
                        yield parse_playlist(self.instance_id, self._client, playlist)
                else:  # emby playlists are only audio type
                    yield parse_playlist(self.instance_id, self._client, playlist)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        if jellyfin_album := await self._client.get_album(prov_album_id):
            return parse_album(self.logger, self.instance_id, self._client, jellyfin_album)
        msg = f"Item {prov_album_id} not found"
        raise MediaNotFoundError(msg)

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        jellyfin_album_tracks = await self._client.tracks(
            prov_album_id, enable_user_data=True, fields=TRACK_FIELDS
        )
        return [
            parse_track(self.logger, self.instance_id, self._client, jellyfin_album_track)
            for jellyfin_album_track in jellyfin_album_tracks["Items"]
        ]

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id == UNKNOWN_ARTIST_MAPPING.item_id:
            artist = Artist(
                item_id=UNKNOWN_ARTIST_MAPPING.item_id,
                name=UNKNOWN_ARTIST_MAPPING.name,
                provider=self.domain,
                provider_mappings={
                    ProviderMapping(
                        item_id=UNKNOWN_ARTIST_MAPPING.item_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            artist.mbid = UNKNOWN_ARTIST_ID_MBID
            return artist

        if jellyfin_artist := await self._client.get_artist(prov_artist_id):
            return parse_artist(self.logger, self.instance_id, self._client, jellyfin_artist)
        msg = f"Item {prov_artist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if jellyfin_track := await self._client.get_track(prov_track_id):
            return parse_track(self.logger, self.instance_id, self._client, jellyfin_track)
        msg = f"Item {prov_track_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        if jellyfin_playlist := await self._client.get_playlist(prov_playlist_id):
            return parse_playlist(self.instance_id, self._client, jellyfin_playlist)
        msg = f"Item {prov_playlist_id} not found"
        raise MediaNotFoundError(msg)

    async def get_playlist_tracks(
        self, prov_playlist_id: str, offset: int, limit: int
    ) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if offset:
            # paging not supported, we always return the whole list at once
            return []
        # TODO: Does Jellyfin support paging here?
        jellyfin_playlist = await self._client.get_playlist(prov_playlist_id)
        playlist_items = await self._client.tracks(
            jellyfin_playlist[ITEM_KEY_ID], enable_user_data=True, fields=TRACK_FIELDS
        )
        if not playlist_items:
            return result
        for index, jellyfin_track in enumerate(playlist_items["Items"], 1):
            try:
                if track := parse_track(
                    self.logger, self.instance_id, self._client, jellyfin_track
                ):
                    if not track.position:
                        track.position = offset + index
                    result.append(track)
            except (KeyError, ValueError) as err:
                self.logger.error(
                    "Skipping track %s: %s", jellyfin_track.get(ITEM_KEY_NAME, index), str(err)
                )
        return result

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        if not prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            return []
        albums = await self._client.albums(
            prov_artist_id, fields=ALBUM_FIELDS, enable_user_data=True
        )
        return [
            parse_album(self.logger, self.instance_id, self._client, album)
            for album in albums["Items"]
        ]

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        jellyfin_track = await self._client.get_track(item_id)
        mimetype = self._media_mime_type(jellyfin_track)
        media_stream = jellyfin_track[ITEM_KEY_MEDIA_STREAMS][0]
        url = self._client.audio_url(jellyfin_track[ITEM_KEY_ID], SUPPORTED_CONTAINER_FORMATS)
        if ITEM_KEY_MEDIA_CODEC in media_stream:
            content_type = ContentType.try_parse(media_stream[ITEM_KEY_MEDIA_CODEC])
        else:
            content_type = ContentType.try_parse(mimetype) if mimetype else ContentType.UNKNOWN
        return StreamDetails(
            item_id=jellyfin_track[ITEM_KEY_ID],
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                channels=jellyfin_track[ITEM_KEY_MEDIA_STREAMS][0][ITEM_KEY_MEDIA_CHANNELS],
            ),
            stream_type=StreamType.HTTP,
            duration=int(
                jellyfin_track[ITEM_KEY_RUNTIME_TICKS] / 10000000
            ),  # 10000000 ticks per millisecond)
            path=url,
        )

    async def _get_music_libraries(self) -> list[JellyMediaLibrary]:
        """Return all supported libraries a user has access to."""
        response = await self._client.get_media_folders()
        libraries = response["Items"]
        result = []
        for library in libraries:
            if ITEM_KEY_COLLECTION_TYPE in library and library[ITEM_KEY_COLLECTION_TYPE] in "music":
                result.append(library)
        return result

    async def _get_playlists(self) -> list[JellyMediaLibrary]:
        """Return all supported libraries a user has access to."""
        response = await self._client.get_media_folders()
        libraries = response["Items"]
        result = []
        for library in libraries:
            if (
                ITEM_KEY_COLLECTION_TYPE in library
                and library[ITEM_KEY_COLLECTION_TYPE] in "playlists"
            ):
                result.append(library)
        return result

    def _media_mime_type(self, media_item: JellyTrack) -> str | None:
        """Return the mime type of a media item."""
        if not media_item.get(ITEM_KEY_MEDIA_SOURCES):
            return None

        media_source = media_item[ITEM_KEY_MEDIA_SOURCES][0]

        if "Path" not in media_source:
            return None

        path = media_source["Path"]
        mime_type, _ = mimetypes.guess_type(path)

        return mime_type
