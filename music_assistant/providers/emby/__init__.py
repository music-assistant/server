"""Emby Music Provider for MusicAssistant."""

from __future__ import annotations

import hashlib
import socket
from asyncio import TaskGroup
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from aiohttp import ClientError, ClientResponseError
from music_assistant_models.enums import (
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderPermissionDenied,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    AudioFormat,
    ItemMapping,
    MediaItemType,
    Playlist,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.mass import MusicAssistant
from music_assistant.models import ProviderInstanceType
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.emby.const import (
    ALBUM_FIELDS,
    ARTIST_FIELDS,
    AUDIOBOOK_FIELDS,
    AUDIOBOOK_PART_FIELDS,
    AUTH_ACCESS_TOKEN,
    AUTH_USER,
    ITEM_KEY_COLLECTION_TYPE,
    ITEM_KEY_ID,
    ITEM_KEY_INDEX_NUMBER,
    ITEM_KEY_MEDIA_STREAMS,
    ITEM_KEY_NAME,
    ITEM_KEY_PARENT_INDEX_NUMBER,
    ITEM_KEY_RUNTIME_TICKS,
    ITEM_KEY_USER_DATA,
    ITEM_LIMIT,
    ITEMS,
    TRACK_FIELDS,
    USER_DATA_KEY_LAST_PLAYED_DATE,
    USER_DATA_KEY_PLAYBACK_POSITION_TICKS,
    USER_DATA_KEY_PLAYED,
)
from music_assistant.providers.emby.parsers import (
    parse_album,
    parse_artist,
    parse_audiobook,
    parse_playlist,
    parse_stream_details,
    parse_track,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import (
    APPLICATION_NAME,
    CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING,
    CONF_IP_ADDRESS,
    CONF_PASSWORD,
    CONF_PROVIDERS,
    CONF_USERNAME,
)

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_AUDIOBOOKS,
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


class EmbyProvider(MusicProvider):
    """Provider for an Emby music library (uses Emby REST API)."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Get configuration entries for provider setup."""
        return ()

    async def handle_async_init(self) -> None:
        """Initialize provider(instance) with given configuration."""
        username = str(self.get_setup_value(CONF_USERNAME))
        password = str(self.get_setup_value(CONF_PASSWORD) or "")
        self._base_url = str(self.get_setup_value(CONF_IP_ADDRESS)).rstrip("/") + "/"
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

    async def sync_library(self, media_type: MediaType) -> None:
        """
        Run library sync for this provider.

        :param media_type: The media type to sync.
        """
        await super().sync_library(media_type)
        # TODO: remove after 2.13 release
        heal_key = (
            f"{CONF_PROVIDERS}/{self.instance_id}/values/{CONF_EMBY_ALBUM_ARTISTS_HEAL_PENDING}"
        )
        if media_type != MediaType.ALBUM or not self.mass.config.get(heal_key):
            return
        try:
            await self._heal_album_artists()
        except (MusicAssistantError, ClientError) as err:
            # the album sync itself succeeded; keep the marker so the next sync retries
            self.logger.warning("Could not unlink featured album artists: %s", err)
            return
        self.mass.config.remove(heal_key)

    async def get_library_audiobooks(self) -> AsyncGenerator[Audiobook]:
        """Yield all audiobooks from the audiobook libraries."""
        libs = await self._get_audiobook_libraries()
        for lib in libs:
            params = {
                "ParentId": lib[ITEM_KEY_ID],
                # Emby indexes audiobooks as album-like items whose part files are
                # their child audio items.
                "IncludeItemTypes": "MusicAlbum",
                "EnableUserData": "true",
                "Fields": ",".join(AUDIOBOOK_FIELDS),
                "Recursive": "true",
            }
            page = 0
            while True:
                params["StartIndex"] = str(page * ITEM_LIMIT)
                params["Limit"] = ITEM_LIMIT
                resp = await self._get(f"Users/{self._user_id}/Items", params=params)
                items = self._get_response_items(resp, "audiobook")
                if not items:
                    break
                for audiobook in items:
                    if not isinstance(audiobook, dict):
                        self.report_skipped_sync_item(
                            MediaType.AUDIOBOOK,
                            None,
                            InvalidDataError("Emby audiobook listing contains a non-object entry"),
                        )
                        continue
                    item_id = audiobook.get(ITEM_KEY_ID)
                    if not isinstance(item_id, str) or not item_id.strip():
                        self.report_skipped_sync_item(
                            MediaType.AUDIOBOOK,
                            None,
                            InvalidDataError("Emby audiobook listing entry has no id"),
                        )
                        continue
                    yield parse_audiobook(self.instance_id, self, audiobook)
                page += 1

    @use_cache(3600)
    async def get_audiobook(self, prov_audiobook_id: str) -> Audiobook:
        """Get full audiobook details (including parts/chapters) by id."""
        item = await self._get(
            f"Users/{self._user_id}/Items/{prov_audiobook_id}",
            params={"EnableUserData": "true", "Fields": ",".join(AUDIOBOOK_FIELDS)},
        )

        return await self._parse_audiobook_item(item)

    async def get_resume_position(
        self, item_id: str, media_type: MediaType
    ) -> tuple[bool, int, datetime | None]:
        """
        Get the progress (resume point) Emby has stored for an audiobook.

        :param item_id: The provider item id of the audiobook.
        :param media_type: The media type (AUDIOBOOK).
        :return: (fully_played, position_ms, timestamp)
        """
        if media_type != MediaType.AUDIOBOOK:
            msg = f"Unsupported media type for resume position: {media_type}"
            raise NotImplementedError(msg)
        item = await self._get(
            f"Users/{self._user_id}/Items/{item_id}",
            params={"EnableUserData": "true"},
        )
        user_data = item.get(ITEM_KEY_USER_DATA, {})
        fully_played = bool(user_data.get(USER_DATA_KEY_PLAYED))
        position_ms = int(user_data.get(USER_DATA_KEY_PLAYBACK_POSITION_TICKS, 0) / 10000)
        timestamp = (
            datetime.fromisoformat(last_played_date)
            if (last_played_date := user_data.get(USER_DATA_KEY_LAST_PLAYED_DATE))
            else None
        )
        return fully_played, position_ms, timestamp

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

    async def _post(self, path: str, json: dict[str, Any] | None = None) -> None:
        url = urljoin(self._base_url, path.lstrip("/"))
        try:
            async with self._session.post(url, headers=self._headers, json=json) as resp:
                resp.raise_for_status()
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
        items = await self._search_items(search_query, "Playlist", ["ImageTags"], limit)
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
        audiobooks = None

        async with TaskGroup() as tg:
            if MediaType.ARTIST in media_types:
                artists = tg.create_task(self._search_artist(search_query, limit))
            if MediaType.ALBUM in media_types:
                albums = tg.create_task(self._search_album(search_query, limit))
            if MediaType.TRACK in media_types:
                tracks = tg.create_task(self._search_track(search_query, limit))
            if MediaType.PLAYLIST in media_types:
                playlists = tg.create_task(self._search_playlist(search_query, limit))
            if MediaType.AUDIOBOOK in media_types:
                audiobooks = tg.create_task(self._search_audiobook(search_query, limit))

        search_results = SearchResults()
        if artists:
            search_results.artists = artists.result()
        if albums:
            search_results.albums = albums.result()
        if tracks:
            search_results.tracks = tracks.result()
        if playlists:
            search_results.playlists = playlists.result()
        if audiobooks:
            search_results.audiobooks = audiobooks.result()
        return search_results

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
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
                resp = await self._get("Artists", params=params)
                items = self._get_response_items(resp, "artist")
                if not items:
                    break
                for artist in items:
                    if not isinstance(artist, dict):
                        self.report_skipped_sync_item(
                            MediaType.ARTIST,
                            None,
                            InvalidDataError("Emby artist listing contains a non-object entry"),
                        )
                        continue
                    item_id = artist.get(ITEM_KEY_ID)
                    if not isinstance(item_id, str) or not item_id.strip():
                        self.report_skipped_sync_item(
                            MediaType.ARTIST,
                            None,
                            InvalidDataError("Emby artist listing entry has no id"),
                        )
                        continue
                    yield parse_artist(self.instance_id, self, artist)
                page += 1

    async def get_library_albums(self) -> AsyncGenerator[Album]:
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
                items = self._get_response_items(resp, "album")
                if not items:
                    break
                for album in items:
                    if not isinstance(album, dict):
                        self.report_skipped_sync_item(
                            MediaType.ALBUM,
                            None,
                            InvalidDataError("Emby album listing contains a non-object entry"),
                        )
                        continue
                    item_id = album.get(ITEM_KEY_ID)
                    if not isinstance(item_id, str) or not item_id.strip():
                        self.report_skipped_sync_item(
                            MediaType.ALBUM,
                            None,
                            InvalidDataError("Emby album listing entry has no id"),
                        )
                        continue
                    yield parse_album(self.instance_id, self, album)
                page += 1

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
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
                items = self._get_response_items(resp, "track")
                if not items:
                    break
                for track in items:
                    if not isinstance(track, dict):
                        self.report_skipped_sync_item(
                            MediaType.TRACK,
                            None,
                            InvalidDataError("Emby track listing contains a non-object entry"),
                        )
                        continue
                    item_id = track.get(ITEM_KEY_ID)
                    if not isinstance(item_id, str) or not item_id.strip():
                        self.report_skipped_sync_item(
                            MediaType.TRACK,
                            None,
                            InvalidDataError("Emby track listing entry has no id"),
                        )
                        continue
                    media_streams = track.get(ITEM_KEY_MEDIA_STREAMS)
                    if not isinstance(media_streams, list) or not media_streams:
                        self.report_skipped_sync_item(
                            MediaType.TRACK,
                            item_id,
                            InvalidDataError("Emby track has no media streams"),
                        )
                        continue
                    yield parse_track(self.instance_id, self, track)
                page += 1

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Yield all playlists from the music library."""
        libs = await self._get_music_libraries()
        for lib in libs:
            params = {
                "ParentId": lib[ITEM_KEY_ID],
                "IncludeItemTypes": "Playlist",
                "EnableUserData": "true",
                "Fields": "ImageTags",
                "Recursive": "true",
            }
            page = 0
            while True:
                params["StartIndex"] = str(page * ITEM_LIMIT)
                params["Limit"] = ITEM_LIMIT
                resp = await self._get(f"Users/{self._user_id}/Items", params=params)
                items = self._get_response_items(resp, "playlist")
                if not items:
                    break
                for playlist in items:
                    if not isinstance(playlist, dict):
                        self.report_skipped_sync_item(
                            MediaType.PLAYLIST,
                            None,
                            InvalidDataError("Emby playlist listing contains a non-object entry"),
                        )
                        continue
                    item_id = playlist.get(ITEM_KEY_ID)
                    if not isinstance(item_id, str) or not item_id.strip():
                        self.report_skipped_sync_item(
                            MediaType.PLAYLIST,
                            None,
                            InvalidDataError("Emby playlist listing entry has no id"),
                        )
                        continue
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
            params={"EnableUserData": "true", "Fields": "ImageTags"},
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
        if media_type == MediaType.AUDIOBOOK:
            return await self._get_audiobook_stream_details(item_id)

        track_data = await self._get(
            f"Users/{self._user_id}/Items/{item_id}",
            params={"EnableUserData": "true", "Fields": ",".join(TRACK_FIELDS)},
        )

        audio_format = parse_stream_details(track_data)

        duration = int(
            track_data.get(ITEM_KEY_RUNTIME_TICKS, 0) / 10000000
        )  # Convert ticks to seconds

        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=audio_format,
            stream_type=StreamType.HTTP,
            duration=duration,
            path=self._audio_url(item_id, audio_format),
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
        return await self._get_libraries_by_collection_type("music")

    async def _get_audiobook_libraries(self) -> list[dict[str, Any]]:
        return await self._get_libraries_by_collection_type("audiobooks")

    async def _get_libraries_by_collection_type(self, collection_type: str) -> list[dict[str, Any]]:
        resp = await self._get("Library/MediaFolders")
        libs = self._get_response_items(resp, "library")
        result = []
        for library in libs:
            if not isinstance(library, dict):
                raise InvalidDataError("Emby media-library response contains a non-object entry")
            if str(library.get(ITEM_KEY_COLLECTION_TYPE, "")).lower() == collection_type:
                result.append(library)
        return result

    async def _search_audiobook(self, search_query: str, limit: int) -> list[Audiobook]:
        items: list[dict[str, Any]] = []
        for lib in await self._get_audiobook_libraries():
            params = {
                "ParentId": lib[ITEM_KEY_ID],
                "SearchTerm": search_query,
                "IncludeItemTypes": "MusicAlbum",
                "EnableUserData": "true",
                "Fields": ",".join(AUDIOBOOK_FIELDS),
                "Limit": str(limit),
                "Recursive": "true",
            }
            resp = await self._get(f"Users/{self._user_id}/Items", params=params)
            items.extend(
                item
                for item in self._get_response_items(resp, "audiobook")
                if isinstance(item, dict)
            )
        return [parse_audiobook(self.instance_id, self, item) for item in items]

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Handle media item played event."""
        if media_type not in (MediaType.TRACK, MediaType.AUDIOBOOK):
            return
        if fully_played:
            await self._post(f"Users/{self._user_id}/PlayedItems/{prov_item_id}")
        if is_playing or (media_type == MediaType.AUDIOBOOK and position > 0 and not fully_played):
            # audiobooks also report their stop position when playback ends,
            # which is the last position Emby gets if the 30s heartbeats never fired.
            # A finished book is reported at its very end, which is no position to
            # resume from and would undo the played mark posted just above.
            await self._post(
                f"/Users/{self._user_id}/Items/{prov_item_id}/UserData",
                json={"PlaybackPositionTicks": position * 10000000},
            )
        if not fully_played and position == 0:
            await self._post(f"/Users/{self._user_id}/PlayedItems/{prov_item_id}/Delete")

    def _audio_url(self, item_id: str, audio_format: AudioFormat) -> str:
        """Build the (static) Emby audio stream url for the given item."""
        url = urljoin(self._base_url, f"Audio/{item_id}/universal")
        params = {
            "Container": audio_format.content_type,
            "AudioCodec": audio_format.codec_type,
            "AudioSampleRate": audio_format.sample_rate,
            "AudioChannels": audio_format.channels,
            "Static": "true",
            "api_key": self._token,
        }
        query = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{url}?{query}"

    async def _parse_audiobook_item(self, item: dict[str, Any]) -> Audiobook:
        """Parse an audiobook item together with its part files."""
        parts = await self._get_audiobook_part_items(str(item.get(ITEM_KEY_ID)))
        return parse_audiobook(self.instance_id, self, item, parts)

    async def _get_audiobook_part_items(self, prov_audiobook_id: str) -> list[dict[str, Any]]:
        """Return the ordered child audio items that make up an audiobook."""
        params = {
            "ParentId": prov_audiobook_id,
            "IncludeItemTypes": "Audio",
            "Fields": ",".join(AUDIOBOOK_PART_FIELDS),
            "Limit": ITEM_LIMIT,
            "Recursive": "true",
        }
        result: list[dict[str, Any]] = []
        page = 0
        while True:
            params["StartIndex"] = str(page * ITEM_LIMIT)
            resp = await self._get(f"Users/{self._user_id}/Items", params=params)
            items = self._get_response_items(resp, "audiobook part")
            result.extend(item for item in items if isinstance(item, dict))
            if len(items) < ITEM_LIMIT:
                break
            page += 1
        # Emby does not guarantee the order of the child listing; disc and track numbers
        # order the files when tagged, the otherwise-ascending file names do when not.
        # The disc number leads because track numbers restart on every disc.
        result.sort(
            key=lambda part: (
                0 if part.get(ITEM_KEY_INDEX_NUMBER) else 1,
                int(part.get(ITEM_KEY_PARENT_INDEX_NUMBER) or 0),
                int(part.get(ITEM_KEY_INDEX_NUMBER) or 0),
                str(part.get(ITEM_KEY_NAME)),
            )
        )
        return result

    async def _get_audiobook_stream_details(self, item_id: str) -> StreamDetails:
        """Build (possibly multi-part) stream details for an audiobook."""
        parts = await self._get_audiobook_part_items(item_id)

        stream_parts: list[MultiPartPath] = []
        for part in parts:
            part_format = parse_stream_details(part)
            # kept as a fraction: summed part durations locate the resume/seek point
            duration = part.get(ITEM_KEY_RUNTIME_TICKS, 0) / 10000000  # ticks to seconds
            stream_parts.append(
                MultiPartPath(
                    path=self._audio_url(str(part.get(ITEM_KEY_ID)), part_format),
                    duration=duration,
                )
            )
        if not stream_parts:
            msg = f"Audiobook {item_id} has no playable parts"
            raise MediaNotFoundError(msg)
        if len(stream_parts) == 1:
            path: str | list[MultiPartPath] = stream_parts[0].path
        else:
            path = stream_parts
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            media_type=MediaType.AUDIOBOOK,
            audio_format=parse_stream_details(parts[0]),
            stream_type=StreamType.HTTP,
            duration=int(sum(p.duration or 0 for p in stream_parts)),
            path=path,
            can_seek=True,
            allow_seek=True,
        )

    @staticmethod
    def _get_response_items(response: dict[str, Any], item_type: str) -> list[Any]:
        """Return the required item list from an Emby listing response."""
        items = response.get(ITEMS)
        if not isinstance(items, list):
            raise InvalidDataError(f"Emby {item_type} response contains no item list")
        return items

    async def _heal_album_artists(self) -> None:
        """Unlink the featured track artists that earlier syncs stored as album artists."""
        removed = 0
        async for prov_album in self.get_library_albums():
            if not prov_album.artists:
                continue
            db_album = await self.mass.music.albums.get_library_item_by_prov_id(
                prov_album.item_id, self.instance_id
            )
            if db_album is None:
                continue
            # full library artists, as linking an ItemMapping on overwrite resets the artist
            keep: dict[str, Artist] = {}
            for prov_artist in prov_album.artists:
                db_artist = await self.mass.music.artists.get_library_item_by_prov_id(
                    prov_artist.item_id, self.instance_id
                )
                if db_artist is None:
                    # an album artist the old sync never linked, e.g. Various Artists
                    new_artist = (
                        self.mass.music.artists.artist_from_item_mapping(prov_artist)
                        if isinstance(prov_artist, ItemMapping)
                        else prov_artist
                    )
                    db_artist = await self.mass.music.artists.add_item_to_library(new_artist)
                keep[str(db_artist.item_id)] = db_artist
            album_is_emby_only = self._only_on_emby(db_album)
            album_removed = 0
            for linked in db_album.artists:
                if str(linked.item_id) in keep:
                    continue
                library_artist = await self.mass.music.artists.get_library_item(linked.item_id)
                if album_is_emby_only or self._only_on_emby(library_artist):
                    album_removed += 1
                    continue
                # another provider knows both the album and the artist and may have linked it
                keep[str(library_artist.item_id)] = library_artist
            if {str(artist.item_id) for artist in db_album.artists} == set(keep):
                continue
            db_album.artists = UniqueList(keep.values())
            # every overwritten column gets the library album's own value, so only artists change
            await self.mass.music.albums.update_item_in_library(
                db_album.item_id, db_album, overwrite=True
            )
            removed += album_removed
        self.logger.info("Unlinked %d featured artist(s) from library albums", removed)

    def _only_on_emby(self, item: Album | Artist) -> bool:
        """Return True if Emby is the item's only provider."""
        # every Emby instance stored its album artists with the same faulty parser
        return all(mapping.provider_domain == self.domain for mapping in item.provider_mappings)
