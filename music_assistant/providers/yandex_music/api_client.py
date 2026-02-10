"""API client wrapper for Yandex Music."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from yandex_music import Album as YandexAlbum
from yandex_music import Artist as YandexArtist
from yandex_music import ClientAsync, Search, TrackShort
from yandex_music import Playlist as YandexPlaylist
from yandex_music import Track as YandexTrack
from yandex_music.exceptions import BadRequestError, NetworkError, UnauthorizedError
from yandex_music.utils.sign_request import get_sign_request

if TYPE_CHECKING:
    from yandex_music import DownloadInfo

from .constants import DEFAULT_LIMIT

# get-file-info with quality=lossless returns FLAC; default /tracks/.../download-info often does not
# Prefer flac-mp4/aac-mp4 (Yandex API moved to these formats around 2025)
GET_FILE_INFO_CODECS = "flac-mp4,flac,aac-mp4,aac,he-aac,mp3,he-aac-mp4"
# get-file-info: same host as library (all requests go through one API)
GET_FILE_INFO_BASE_URL = "https://api.music.yandex.net"

LOGGER = logging.getLogger(__name__)


class YandexMusicClient:
    """Wrapper around yandex-music-api ClientAsync."""

    def __init__(self, token: str) -> None:
        """Initialize the Yandex Music client.

        :param token: Yandex Music OAuth token.
        """
        self._token = token
        self._client: ClientAsync | None = None
        self._user_id: int | None = None

    @property
    def user_id(self) -> int:
        """Return the user ID."""
        if self._user_id is None:
            raise ProviderUnavailableError("Client not initialized, call connect() first")
        return self._user_id

    async def connect(self) -> bool:
        """Initialize the client and verify token validity.

        :return: True if connection was successful.
        :raises LoginFailed: If the token is invalid.
        """
        try:
            self._client = await ClientAsync(self._token).init()
            if self._client.me is None or self._client.me.account is None:
                raise LoginFailed("Failed to get account info")
            self._user_id = self._client.me.account.uid
            LOGGER.debug("Connected to Yandex Music as user %s", self._user_id)
            return True
        except UnauthorizedError as err:
            raise LoginFailed("Invalid Yandex Music token") from err
        except NetworkError as err:
            msg = "Network error connecting to Yandex Music"
            raise ResourceTemporarilyUnavailable(msg) from err

    async def disconnect(self) -> None:
        """Disconnect the client."""
        self._client = None
        self._user_id = None

    def _ensure_connected(self) -> ClientAsync:
        """Ensure the client is connected and return it."""
        if self._client is None:
            raise ProviderUnavailableError("Client not connected, call connect() first")
        return self._client

    # Library methods

    async def get_liked_tracks(self) -> list[TrackShort]:
        """Get user's liked tracks.

        :return: List of liked track objects.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_likes_tracks()
            if result is None:
                return []
            return result.tracks or []
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching liked tracks: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked tracks") from err

    async def get_liked_albums(self) -> list[YandexAlbum]:
        """Get user's liked albums with full details (including cover art).

        The users_likes_albums endpoint returns minimal album data without
        cover_uri, so we fetch full album details in batches afterwards.

        :return: List of liked album objects with full details.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_likes_albums()
            if result is None:
                return []
            album_ids = [
                str(like.album.id) for like in result if like.album is not None and like.album.id
            ]
            if not album_ids:
                return []
            # Fetch full album details in batches to get cover_uri and other metadata
            batch_size = 50
            full_albums: list[YandexAlbum] = []
            for i in range(0, len(album_ids), batch_size):
                batch = album_ids[i : i + batch_size]
                try:
                    batch_result = await client.albums(batch)
                    if batch_result:
                        full_albums.extend(batch_result)
                except (BadRequestError, NetworkError) as batch_err:
                    LOGGER.warning("Error fetching album details batch: %s", batch_err)
                    # Fall back to minimal data for this batch
                    batch_set = set(batch)
                    for like in result:
                        if (
                            like.album is not None
                            and like.album.id
                            and str(like.album.id) in batch_set
                        ):
                            full_albums.append(like.album)
            return full_albums
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching liked albums: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked albums") from err

    async def get_liked_artists(self) -> list[YandexArtist]:
        """Get user's liked artists.

        :return: List of liked artist objects.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_likes_artists()
            if result is None:
                return []
            return [like.artist for like in result if like.artist is not None]
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching liked artists: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked artists") from err

    async def get_user_playlists(self) -> list[YandexPlaylist]:
        """Get user's playlists.

        :return: List of playlist objects.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_playlists_list()
            if result is None:
                return []
            return list(result)
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching playlists: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch playlists") from err

    # Search

    async def search(
        self,
        query: str,
        search_type: str = "all",
        limit: int = DEFAULT_LIMIT,
    ) -> Search | None:
        """Search for tracks, albums, artists, or playlists.

        :param query: Search query string.
        :param search_type: Type of search ('all', 'track', 'album', 'artist', 'playlist').
        :param limit: Maximum number of results per type.
        :return: Search results object.
        """
        client = self._ensure_connected()
        try:
            return await client.search(query, type_=search_type, page=0, nocorrect=False)
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Search error: %s", err)
            raise ResourceTemporarilyUnavailable("Search failed") from err

    # Get single items

    async def get_track(self, track_id: str) -> YandexTrack | None:
        """Get a single track by ID.

        :param track_id: Track ID.
        :return: Track object or None if not found.
        """
        client = self._ensure_connected()
        try:
            tracks = await client.tracks([track_id])
            return tracks[0] if tracks else None
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching track %s: %s", track_id, err)
            return None

    async def get_tracks(self, track_ids: list[str]) -> list[YandexTrack]:
        """Get multiple tracks by IDs.

        :param track_ids: List of track IDs.
        :return: List of track objects.
        :raises ResourceTemporarilyUnavailable: On network errors after retry.
        """
        client = self._ensure_connected()
        try:
            result = await client.tracks(track_ids)
            return result or []
        except NetworkError as err:
            # Retry once on network errors (timeout, disconnect, etc.)
            LOGGER.warning("Network error fetching tracks, retrying once: %s", err)
            try:
                result = await client.tracks(track_ids)
                return result or []
            except NetworkError as retry_err:
                LOGGER.error("Error fetching tracks (retry failed): %s", retry_err)
                raise ResourceTemporarilyUnavailable("Failed to fetch tracks") from retry_err
        except BadRequestError as err:
            LOGGER.error("Error fetching tracks: %s", err)
            return []

    async def get_album(self, album_id: str) -> YandexAlbum | None:
        """Get a single album by ID.

        :param album_id: Album ID.
        :return: Album object or None if not found.
        """
        client = self._ensure_connected()
        try:
            albums = await client.albums([album_id])
            return albums[0] if albums else None
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching album %s: %s", album_id, err)
            return None

    async def get_album_with_tracks(self, album_id: str) -> YandexAlbum | None:
        """Get an album with its tracks.

        Uses the same semantics as the web client: albums/{id}/with-tracks
        with resumeStream, richTracks, withListeningFinished when the library
        passes them through.

        :param album_id: Album ID.
        :return: Album object with tracks or None if not found.
        """
        client = self._ensure_connected()
        try:
            return await client.albums_with_tracks(
                album_id,
                resumeStream=True,
                richTracks=True,
                withListeningFinished=True,
            )
        except TypeError:
            # Older yandex-music may not accept these kwargs
            return await client.albums_with_tracks(album_id)
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching album with tracks %s: %s", album_id, err)
            return None

    async def get_artist(self, artist_id: str) -> YandexArtist | None:
        """Get a single artist by ID.

        :param artist_id: Artist ID.
        :return: Artist object or None if not found.
        """
        client = self._ensure_connected()
        try:
            artists = await client.artists([artist_id])
            return artists[0] if artists else None
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching artist %s: %s", artist_id, err)
            return None

    async def get_artist_albums(
        self, artist_id: str, limit: int = DEFAULT_LIMIT
    ) -> list[YandexAlbum]:
        """Get artist's albums.

        :param artist_id: Artist ID.
        :param limit: Maximum number of albums.
        :return: List of album objects.
        """
        client = self._ensure_connected()
        try:
            result = await client.artists_direct_albums(artist_id, page=0, page_size=limit)
            if result is None:
                return []
            return result.albums or []
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching artist albums %s: %s", artist_id, err)
            return []

    async def get_artist_tracks(
        self, artist_id: str, limit: int = DEFAULT_LIMIT
    ) -> list[YandexTrack]:
        """Get artist's top tracks.

        :param artist_id: Artist ID.
        :param limit: Maximum number of tracks.
        :return: List of track objects.
        """
        client = self._ensure_connected()
        try:
            result = await client.artists_tracks(artist_id, page=0, page_size=limit)
            if result is None:
                return []
            return result.tracks or []
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching artist tracks %s: %s", artist_id, err)
            return []

    async def get_playlist(self, user_id: str, playlist_id: str) -> YandexPlaylist | None:
        """Get a playlist by ID.

        :param user_id: User ID (owner of the playlist).
        :param playlist_id: Playlist ID (kind).
        :return: Playlist object or None if not found.
        :raises ResourceTemporarilyUnavailable: On network errors.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_playlists(kind=int(playlist_id), user_id=user_id)
            if isinstance(result, list):
                return result[0] if result else None
            return result
        except NetworkError as err:
            LOGGER.warning("Network error fetching playlist %s/%s: %s", user_id, playlist_id, err)
            raise ResourceTemporarilyUnavailable("Failed to fetch playlist") from err
        except BadRequestError as err:
            LOGGER.error("Error fetching playlist %s/%s: %s", user_id, playlist_id, err)
            return None

    # Streaming

    async def get_track_download_info(
        self, track_id: str, get_direct_links: bool = True
    ) -> list[DownloadInfo]:
        """Get download info for a track.

        :param track_id: Track ID.
        :param get_direct_links: Whether to get direct download links.
        :return: List of download info objects.
        """
        client = self._ensure_connected()
        try:
            result = await client.tracks_download_info(track_id, get_direct_links=get_direct_links)
            return result or []
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error fetching download info for track %s: %s", track_id, err)
            return []

    async def get_track_file_info_lossless(self, track_id: str) -> dict[str, Any] | None:
        """Request lossless stream via get-file-info (quality=lossless).

        The /tracks/{id}/download-info endpoint often returns only MP3; get-file-info
        with quality=lossless and codecs=flac,... returns FLAC when available.

        :param track_id: Track ID.
        :return: Parsed downloadInfo dict (url, codec, urls, ...) or None on error.
        """
        client = self._ensure_connected()
        sign = get_sign_request(track_id)
        base_params = {
            "ts": sign.timestamp,
            "trackId": track_id,
            "quality": "lossless",
            "codecs": GET_FILE_INFO_CODECS,
            "sign": sign.value,
        }

        def _parse_file_info_result(raw: dict[str, Any] | None) -> dict[str, Any] | None:
            if not raw or not isinstance(raw, dict):
                return None
            download_info = raw.get("download_info")
            if not download_info or not download_info.get("url"):
                return None
            return cast("dict[str, Any]", download_info)

        url = f"{GET_FILE_INFO_BASE_URL}/get-file-info"
        params_encraw = {**base_params, "transports": "encraw"}
        try:
            result = await client._request.get(url, params=params_encraw)
            return _parse_file_info_result(result)
        except (BadRequestError, NetworkError) as err:
            LOGGER.debug(
                "get-file-info lossless for track %s: %s %s",
                track_id,
                type(err).__name__,
                getattr(err, "message", str(err)) or repr(err),
            )
            return None
        except UnauthorizedError as err:
            LOGGER.debug(
                "get-file-info lossless for track %s (transports=encraw): %s %s",
                track_id,
                type(err).__name__,
                getattr(err, "message", str(err)) or repr(err),
            )
            LOGGER.debug(
                "If you have Yandex Music Plus and this track has lossless, "
                "try a token from the web client (music.yandex.ru)."
            )
            params_raw = {**base_params, "transports": "raw"}
            try:
                result = await client._request.get(url, params=params_raw)
                return _parse_file_info_result(result)
            except (BadRequestError, NetworkError, UnauthorizedError) as retry_err:
                LOGGER.debug(
                    "get-file-info lossless for track %s (transports=raw): %s %s",
                    track_id,
                    type(retry_err).__name__,
                    getattr(retry_err, "message", str(retry_err)) or repr(retry_err),
                )
                return None

    # Library modifications

    async def like_track(self, track_id: str) -> bool:
        """Add a track to liked tracks.

        :param track_id: Track ID to like.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_likes_tracks_add(track_id)
            return result is not None
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error liking track %s: %s", track_id, err)
            return False

    async def unlike_track(self, track_id: str) -> bool:
        """Remove a track from liked tracks.

        :param track_id: Track ID to unlike.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_likes_tracks_remove(track_id)
            return result is not None
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error unliking track %s: %s", track_id, err)
            return False

    async def like_album(self, album_id: str) -> bool:
        """Add an album to liked albums.

        :param album_id: Album ID to like.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_likes_albums_add(album_id)
            return result is not None
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error liking album %s: %s", album_id, err)
            return False

    async def unlike_album(self, album_id: str) -> bool:
        """Remove an album from liked albums.

        :param album_id: Album ID to unlike.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_likes_albums_remove(album_id)
            return result is not None
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error unliking album %s: %s", album_id, err)
            return False

    async def like_artist(self, artist_id: str) -> bool:
        """Add an artist to liked artists.

        :param artist_id: Artist ID to like.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_likes_artists_add(artist_id)
            return result is not None
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error liking artist %s: %s", artist_id, err)
            return False

    async def unlike_artist(self, artist_id: str) -> bool:
        """Remove an artist from liked artists.

        :param artist_id: Artist ID to unlike.
        :return: True if successful.
        """
        client = self._ensure_connected()
        try:
            result = await client.users_likes_artists_remove(artist_id)
            return result is not None
        except (BadRequestError, NetworkError) as err:
            LOGGER.error("Error unliking artist %s: %s", artist_id, err)
            return False
