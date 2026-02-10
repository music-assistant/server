"""Emby Music Provider for MusicAssistant."""

from __future__ import annotations

import hashlib
import socket
from asyncio import TaskGroup
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from aiohttp import ClientResponseError
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ProviderPermissionDenied,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    Playlist,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.emby.const import (
    ALBUM_FIELDS,
    ARTIST_FIELDS,
    AUTH_ACCESS_TOKEN,
    AUTH_USER,
    ITEM_KEY_COLLECTION_TYPE,
    ITEM_KEY_ID,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_LIMIT,
    ITEMS,
    SUPPORTED_CONTAINER_FORMATS,
    TRACK_FIELDS,
)
from music_assistant.providers.emby.parsers import (
    parse_album,
    parse_artist,
    parse_playlist,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import (
    APPLICATION_NAME,
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_USERNAME,
)

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return EmbyProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Get configuration entries for provider setup."""
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_IP_ADDRESS,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The url of the Emby server to connect to.",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
            description="The username to authenticate to the remote server.",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            description="The password to authenticate to the remote server.",
        ),
    )


class EmbyProvider(MusicProvider):
    """Provider for an Emby music library (uses Emby REST API)."""

    async def handle_async_init(self) -> None:
        """Initialize provider(instance) with given configuration."""
        username = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD) or "")
        self._base_url = str(self.config.get_value(CONF_IP_ADDRESS)).rstrip("/") + "/"
        self._session = self.mass.http_session

        # stable device id
        device_id = hashlib.sha256(f"{self.mass.server_id}+{username}".encode()).hexdigest()
        self._device_id = device_id
        self._device_name = socket.gethostname()

        # authenticate against Emby /Users/AuthenticateByName
        auth_url = urljoin(self._base_url, "Users/AuthenticateByName")
        payload = {"Username": username, "Pw": password}
        headers = {
            "Accept": "application/json",
            "X-Emby-Authorization": (
                f'MediaBrowser Client="{APPLICATION_NAME}", '
                f'Device="{self._device_name}", '
                f'DeviceId="{device_id}", '
                f'Version="{self.mass.version}"'
            ),
        }
        try:
            async with self._session.post(auth_url, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except ClientResponseError as err:
            if err.status == 401:
                raise LoginFailed("Unauthorized: invalid credentials") from err
            if err.status == 403:
                raise ProviderPermissionDenied("Forbidden: insufficient permissions") from err
            if err.status == 404:
                raise MediaNotFoundError("Authentication endpoint not found") from err
            raise

        # store token and user id
        token = data.get(AUTH_ACCESS_TOKEN)
        user = data.get(AUTH_USER)
        if not token or not user:
            raise LoginFailed("Authentication failed: missing token/user in response")
        self._token = token
        self._user_id = user.get(ITEM_KEY_ID)
        self._headers = {
            "Accept": "application/json",
            "X-Emby-Token": self._token,
            "X-Emby-Authorization": (
                f'MediaBrowser Client="{APPLICATION_NAME}", '
                f'Device="{self._device_name}", '
                f'DeviceId="{device_id}", '
                f'Version="{self.mass.version}", '
                f'Token="{self._token}"'
            ),
        }

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if provider supports streaming."""
        return False

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = urljoin(self._base_url, path.lstrip("/"))
        try:
            async with self._session.get(url, headers=self._headers, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()  # type: ignore[no-any-return]
        except ClientResponseError as err:
            if err.status == 401:
                raise LoginFailed("Unauthorized: invalid credentials") from err
            if err.status == 403:
                raise ProviderPermissionDenied("Forbidden: insufficient permissions") from err
            if err.status == 404:
                raise MediaNotFoundError(f"Item {path} not found") from err
            raise

    async def _search_items(
        self, search_query: str, include_types: str, fields: list[str], limit: int
    ) -> list[dict[str, Any]]:
        params = {
            "SearchTerm": search_query,
            "IncludeItemTypes": include_types,
            "EnableUserData": "true",
            "Fields": ",".join(fields or []),
            "Limit": str(limit),
            "Recursive": "true",
        }
        resp = await self._get(f"Users/{self._user_id}/Items", params=params)
        return resp.get(ITEMS, [])  # type: ignore[no-any-return]

    async def _search_track(self, search_query: str, limit: int) -> list[Track]:
        items = await self._search_items(search_query, "Audio", TRACK_FIELDS, limit)
        return [parse_track(self.instance_id, self, item) for item in items]

    async def _search_album(self, search_query: str, limit: int) -> list[Album]:
        albumname = search_query.split(" - ", 1)[1] if " - " in search_query else search_query
        items = await self._search_items(albumname, "MusicAlbum", ALBUM_FIELDS, limit)
        return [parse_album(self.instance_id, self, item) for item in items]

    async def _search_artist(self, search_query: str, limit: int) -> list[Artist]:
        items = await self._search_items(search_query, "MusicArtist", ARTIST_FIELDS, limit)
        return [parse_artist(self.instance_id, self, item) for item in items]

    async def _search_playlist(self, search_query: str, limit: int) -> list[Playlist]:
        items = await self._search_items(search_query, "Playlist", [], limit)
        return [parse_playlist(self.instance_id, self, item) for item in items]

    @use_cache(60 * 15)
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """Search for media items in the Emby library."""
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
        """Yield all artists from the music library."""
        libs = await self._get_music_libraries()
        for lib in libs:
            params = {
                "ParentId": lib[ITEM_KEY_ID],
                "IncludeItemTypes": "MusicArtist",
                "EnableUserData": "true",
                "Fields": ",".join(ARTIST_FIELDS),
                "Recursive": "true",
            }
            page = 0
            while True:
                params["StartIndex"] = str(page * ITEM_LIMIT)
                params["Limit"] = ITEM_LIMIT
                resp = await self._get(f"Users/{self._user_id}/Items", params=params)
                items = resp.get(ITEMS, [])
                if not items:
                    break
                for artist in items:
                    yield parse_artist(self.instance_id, self, artist)
                page += 1

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Yield all albums from the music library."""
        libs = await self._get_music_libraries()
        for lib in libs:
            params = {
                "ParentId": lib[ITEM_KEY_ID],
                "IncludeItemTypes": "MusicAlbum",
                "EnableUserData": "true",
                "Fields": ",".join(ALBUM_FIELDS),
                "Recursive": "true",
            }
            page = 0
            while True:
                params["StartIndex"] = str(page * ITEM_LIMIT)
                params["Limit"] = ITEM_LIMIT
                resp = await self._get(f"Users/{self._user_id}/Items", params=params)
                items = resp.get(ITEMS, [])
                if not items:
                    break
                for album in items:
                    yield parse_album(self.instance_id, self, album)
                page += 1

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Yield all tracks from the music library."""
        libs = await self._get_music_libraries()
        for lib in libs:
            params = {
                "ParentId": lib[ITEM_KEY_ID],
                "IncludeItemTypes": "Audio",
                "EnableUserData": "true",
                "Fields": ",".join(TRACK_FIELDS),
                "Recursive": "true",
            }
            page = 0
            while True:
                params["StartIndex"] = str(page * ITEM_LIMIT)
                params["Limit"] = ITEM_LIMIT
                resp = await self._get(f"Users/{self._user_id}/Items", params=params)
                items = resp.get(ITEMS, [])
                if not items:
                    break
                for track in items:
                    if not len(track.get(ITEM_KEY_MEDIA_STREAMS, [])):
                        continue
                    yield parse_track(self.instance_id, self, track)
                page += 1

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Yield all playlists from the music library."""
        libs = await self._get_music_libraries()
        for lib in libs:
            params = {
                "ParentId": lib[ITEM_KEY_ID],
                "IncludeItemTypes": "Playlist",
                "EnableUserData": "true",
                "Recursive": "true",
            }
            page = 0
            while True:
                params["StartIndex"] = str(page * ITEM_LIMIT)
                params["Limit"] = ITEM_LIMIT
                resp = await self._get(f"Users/{self._user_id}/Items", params=params)
                items = resp.get(ITEMS, [])
                if not items:
                    break
                for playlist in items:
                    yield parse_playlist(self.instance_id, self, playlist)
                page += 1

    @use_cache(3600)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get album by provider album id."""
        album = await self._get(
            f"Users/{self._user_id}/Items/{prov_album_id}",
            params={
                "EnableUserData": "true",
                "Fields": ",".join(ALBUM_FIELDS),
                "Recursive": "true",
            },
        )
        return parse_album(self.instance_id, self, album)

    @use_cache(3600)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get tracks for a given album by provider album id."""
        params = {
            "ParentId": prov_album_id,
            "IncludeItemTypes": "Audio",
            "EnableUserData": "true",
            "Fields": ",".join(TRACK_FIELDS),
            "Limit": ITEM_LIMIT,
            "Recursive": "true",
        }
        resp = await self._get(f"Users/{self._user_id}/Items", params=params)
        return [parse_track(self.instance_id, self, item) for item in resp.get(ITEMS, [])]

    @use_cache(60 * 15)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist by provider artist id."""
        artist_data = await self._get(
            f"Users/{self._user_id}/Items/{prov_artist_id}",
            params={"EnableUserData": "true", "Fields": ",".join(ARTIST_FIELDS)},
        )

        return parse_artist(self.instance_id, self, artist_data)

    @use_cache(3600)
    async def get_artist_toptracks(self, prov_artist_id: str, limit: int = 25) -> list[Track]:
        """Get top tracks for a given artist by provider artist id."""
        params = {
            "ArtistIds": prov_artist_id,
            "IncludeItemTypes": "Audio",
            "EnableUserData": "true",
            "Fields": ",".join(TRACK_FIELDS),
            "Recursive": "true",
            "Limit": str(limit),
            "SortBy": "PlayCount",
            "SortOrder": "Descending",
        }
        resp = await self._get(f"Users/{self._user_id}/Items", params=params)
        return [parse_track(self.instance_id, self, item) for item in resp.get(ITEMS, [])]

    @use_cache(60 * 15)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get track by provider track id."""
        track = await self._get(
            f"Users/{self._user_id}/Items/{prov_track_id}",
            params={"EnableUserData": "true", "Fields": ",".join(TRACK_FIELDS)},
        )

        return parse_track(self.instance_id, self, track)

    @use_cache(60 * 15)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get playlist by provider playlist id."""
        playlist = await self._get(
            f"Users/{self._user_id}/Items/{prov_playlist_id}",
            params={"EnableUserData": "true"},
        )

        return parse_playlist(self.instance_id, self, playlist)

    @use_cache(3600)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get tracks for a given playlist by provider playlist id."""
        result: list[Track] = []
        params = {
            "ParentId": prov_playlist_id,
            "IncludeItemTypes": "Audio",
            "EnableUserData": "true",
            "Fields": ",".join(TRACK_FIELDS),
            "Limit": ITEM_LIMIT,
            "StartIndex": str(page * ITEM_LIMIT),
        }
        resp = await self._get(f"Users/{self._user_id}/Items", params=params)
        for index, item in enumerate(resp.get(ITEMS, []), 1):
            pos = (page * ITEM_LIMIT) + index
            if track := parse_track(self.instance_id, self, item):
                track.position = pos
                result.append(track)

        return result

    @use_cache(3600)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get albums for a given artist by provider artist id."""
        params = {
            "AlbumArtistIds": prov_artist_id,
            "IncludeItemTypes": "MusicAlbum",
            "Fields": ",".join(ALBUM_FIELDS),
            "EnableUserData": "true",
            "Recursive": "true",
        }
        resp = await self._get(f"Users/{self._user_id}/Items", params=params)
        return [parse_album(self.instance_id, self, album) for album in resp.get(ITEMS, [])]

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for given item id and media type."""
        track = await self.get_track(item_id)
        # build universal audio URL (include token as query param for convenience)
        container = ",".join(SUPPORTED_CONTAINER_FORMATS)
        url = urljoin(self._base_url, f"Audio/{track.item_id}/universal")
        params = {"Container": container, "api_key": self._token}
        query = "&".join([f"{k}={v}" for k, v in params.items()])
        return StreamDetails(
            item_id=track.item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(),
            stream_type=StreamType.HTTP,
            duration=int(track.duration) if getattr(track, "duration", None) else 0,
            path=f"{url}?{query}",
            can_seek=True,
            allow_seek=True,
        )

    @use_cache(3600)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks."""
        resp = await self._get(
            f"Items/{prov_track_id}/Similar",
            params={"Limit": str(limit), "Fields": ",".join(TRACK_FIELDS)},
        )

        return [parse_track(self.instance_id, self, t) for t in resp.get(ITEMS, [])]

    async def _get_music_libraries(self) -> list[dict[str, Any]]:
        resp = await self._get("Library/MediaFolders")
        libs = resp.get(ITEMS, [])
        result = []
        for library in libs:
            if ITEM_KEY_COLLECTION_TYPE in library:
                collection_type = library.get(ITEM_KEY_COLLECTION_TYPE, "").lower()
                if collection_type == "music":
                    result.append(library)
        return result
