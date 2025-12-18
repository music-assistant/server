"""Jellyfin support for MusicAssistant."""

from __future__ import annotations

import hashlib
import socket
from asyncio import TaskGroup
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from aiojellyfin import MediaLibrary as JellyMediaLibrary
from aiojellyfin import NotFound, authenticate_by_name
from aiojellyfin.session import SessionConfiguration
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import UNKNOWN_ARTIST_ID_MBID
from music_assistant.controllers.cache import use_cache
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.jellyfin.parsers import (
    audio_format,
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

from .const import (
    ALBUM_FIELDS,
    ARTIST_FIELDS,
    ITEM_KEY_COLLECTION_TYPE,
    ITEM_KEY_ID,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_NAME,
    ITEM_KEY_RUNTIME_TICKS,
    SUPPORTED_CONTAINER_FORMATS,
    TRACK_FIELDS,
    UNKNOWN_ARTIST_MAPPING,
    USER_APP_NAME,
)

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

CONF_URL = "url"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_VERIFY_SSL = "verify_ssl"
FAKE_ARTIST_PREFIX = "_fake://"

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.SIMILAR_TRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return JellyfinProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
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
        username = str(self.config.get_value(CONF_USERNAME))

        # Device ID should be stable between reboots
        # Otherwise every time the provider starts we "leak" a new device
        # entry in the Jellyfin backend, which creates devices and entities
        # in HA if they also use the Jellyfin integration there.

        # We follow a suggestion a Jellyfin dev gave to HA and use an ID
        # that is stable even if provider is removed and re-added.
        # They said mix in username in case the same device/app has 2
        # connections to the same servers

        # Neither of these are secrets (username is handed over to mint a
        # token and server_id is used in zeroconf) but hash them anyway as its meant
        # to be an opaque identifier

        device_id = hashlib.sha256(f"{self.mass.server_id}+{username}".encode()).hexdigest()
        verify_ssl = bool(self.config.get_value(CONF_VERIFY_SSL))
        http_session = self.mass.http_session if verify_ssl else self.mass.http_session_no_ssl

        session_config = SessionConfiguration(
            session=http_session,
            url=str(self.config.get_value(CONF_URL)),
            verify_ssl=bool(self.config.get_value(CONF_VERIFY_SSL)),
            app_name=USER_APP_NAME,
            app_version=self.mass.version,
            device_name=socket.gethostname(),
            device_id=device_id,
        )

        try:
            self._client = await authenticate_by_name(
                session_config,
                username,
                str(self.config.get_value(CONF_PASSWORD)),
            )
        except Exception as err:
            raise LoginFailed(f"Authentication failed: {err}") from err

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return False

    async def _search_track(self, search_query: str, limit: int) -> list[Track]:
        resultset = (
            await self._client.tracks.search_term(search_query)
            .limit(limit)
            .enable_userdata()
            .fields(*TRACK_FIELDS)
            .request()
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
        resultset = (
            await self._client.albums.search_term(albumname)
            .limit(limit)
            .enable_userdata()
            .fields(*ALBUM_FIELDS)
            .request()
        )
        albums = []
        for item in resultset["Items"]:
            albums.append(parse_album(self.logger, self.instance_id, self._client, item))
        return albums

    async def _search_artist(self, search_query: str, limit: int) -> list[Artist]:
        resultset = (
            await self._client.artists.search_term(search_query)
            .limit(limit)
            .enable_userdata()
            .fields(*ARTIST_FIELDS)
            .request()
        )
        artists = []
        for item in resultset["Items"]:
            artists.append(parse_artist(self.logger, self.instance_id, self._client, item))
        return artists

    async def _search_playlist(self, search_query: str, limit: int) -> list[Playlist]:
        resultset = (
            await self._client.playlists.search_term(search_query)
            .limit(limit)
            .enable_userdata()
            .request()
        )
        playlists = []
        for item in resultset["Items"]:
            playlists.append(parse_playlist(self.instance_id, self._client, item))
        return playlists

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """Perform search on the Jellyfin library.

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
            search_results.artists = artists.result()
        if albums:
            search_results.albums = albums.result()
        if tracks:
            search_results.tracks = tracks.result()
        if playlists:
            search_results.playlists = playlists.result()

        return search_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            stream = (
                self._client.artists.parent(jellyfin_library[ITEM_KEY_ID])
                .enable_userdata()
                .fields(*ARTIST_FIELDS)
                .stream(100)
            )
            async for artist in stream:
                yield parse_artist(self.logger, self.instance_id, self._client, artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            stream = (
                self._client.albums.parent(jellyfin_library[ITEM_KEY_ID])
                .enable_userdata()
                .fields(*ALBUM_FIELDS)
                .stream(100)
            )
            async for album in stream:
                yield parse_album(self.logger, self.instance_id, self._client, album)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Jellyfin Music."""
        jellyfin_libraries = await self._get_music_libraries()
        for jellyfin_library in jellyfin_libraries:
            stream = (
                self._client.tracks.parent(jellyfin_library[ITEM_KEY_ID])
                .enable_userdata()
                .fields(*TRACK_FIELDS)
                .stream(100)
            )
            async for track in stream:
                if not len(track[ITEM_KEY_MEDIA_STREAMS]):
                    self.logger.warning(
                        "Invalid track %s: Does not have any media streams", track[ITEM_KEY_NAME]
                    )
                    continue
                yield parse_track(self.logger, self.instance_id, self._client, track)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlist_libraries = await self._get_playlists()
        for playlist_library in playlist_libraries:
            stream = (
                self._client.playlists.parent(playlist_library[ITEM_KEY_ID])
                .enable_userdata()
                .stream(100)
            )
            async for playlist in stream:
                if "MediaType" in playlist:  # Only jellyfin has this property
                    if playlist["MediaType"] == "Audio":
                        yield parse_playlist(self.instance_id, self._client, playlist)
                else:  # emby playlists are only audio type
                    yield parse_playlist(self.instance_id, self._client, playlist)

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        try:
            album = await self._client.get_album(prov_album_id)
        except NotFound:
            raise MediaNotFoundError(f"Item {prov_album_id} not found")
        return parse_album(self.logger, self.instance_id, self._client, album)

    @use_cache(3600)  # Cache for 1 hour
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        jellyfin_album_tracks = (
            await self._client.tracks.parent(prov_album_id)
            .enable_userdata()
            .fields(*TRACK_FIELDS)
            .request()
        )
        return [
            parse_track(self.logger, self.instance_id, self._client, jellyfin_album_track)
            for jellyfin_album_track in jellyfin_album_tracks["Items"]
        ]

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id == UNKNOWN_ARTIST_MAPPING.item_id:
            artist = Artist(
                item_id=UNKNOWN_ARTIST_MAPPING.item_id,
                name=UNKNOWN_ARTIST_MAPPING.name,
                provider=self.instance_id,
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

        try:
            jellyfin_artist = await self._client.get_artist(prov_artist_id)
        except NotFound:
            raise MediaNotFoundError(f"Item {prov_artist_id} not found")
        return parse_artist(self.logger, self.instance_id, self._client, jellyfin_artist)

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        try:
            track = await self._client.get_track(prov_track_id)
        except NotFound:
            raise MediaNotFoundError(f"Item {prov_track_id} not found")
        return parse_track(self.logger, self.instance_id, self._client, track)

    @use_cache(60 * 15)  # Cache for 15 minutes
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        try:
            playlist = await self._client.get_playlist(prov_playlist_id)
        except NotFound:
            raise MediaNotFoundError(f"Item {prov_playlist_id} not found")
        return parse_playlist(self.instance_id, self._client, playlist)

    @use_cache(3600)  # Cache for 1 hour
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        playlist_items = (
            await self._client.tracks.in_playlist(prov_playlist_id)
            .enable_userdata()
            .fields(*TRACK_FIELDS)
            .limit(100)
            .start_index(page * 100)
            .request()
        )
        for index, jellyfin_track in enumerate(playlist_items["Items"], 1):
            pos = (page * 100) + index
            try:
                if track := parse_track(
                    self.logger, self.instance_id, self._client, jellyfin_track
                ):
                    track.position = pos
                    result.append(track)
            except (KeyError, ValueError) as err:
                self.logger.error(
                    "Skipping track %s: %s", jellyfin_track.get(ITEM_KEY_NAME, index), str(err)
                )
        return result

    @use_cache(3600)  # Cache for 1 hour
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        if not prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            return []
        albums = (
            await self._client.albums.parent(prov_artist_id)
            .fields(*ALBUM_FIELDS)
            .enable_userdata()
            .request()
        )
        return [
            parse_album(self.logger, self.instance_id, self._client, album)
            for album in albums["Items"]
        ]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track when it will be streamed."""
        jellyfin_track = await self._client.get_track(item_id)
        url = self._client.audio_url(
            jellyfin_track[ITEM_KEY_ID], container=SUPPORTED_CONTAINER_FORMATS
        )
        return StreamDetails(
            item_id=jellyfin_track[ITEM_KEY_ID],
            provider=self.instance_id,
            audio_format=audio_format(jellyfin_track),
            stream_type=StreamType.HTTP,
            duration=int(
                jellyfin_track[ITEM_KEY_RUNTIME_TICKS] / 10000000
            ),  # 10000000 ticks per millisecond)
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    @use_cache(3600)  # Cache for 1 hour
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        resp = await self._client.get_similar_tracks(
            prov_track_id, limit=limit, fields=TRACK_FIELDS
        )
        return [
            parse_track(self.logger, self.instance_id, self._client, track)
            for track in resp["Items"]
        ]

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
