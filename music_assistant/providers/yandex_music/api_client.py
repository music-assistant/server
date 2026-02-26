"""API client wrapper for Yandex Music."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
)
from yandex_music import Album as YandexAlbum
from yandex_music import Artist as YandexArtist
from yandex_music import ClientAsync, MixLink, Search, TrackShort
from yandex_music import Playlist as YandexPlaylist
from yandex_music import Track as YandexTrack
from yandex_music.exceptions import BadRequestError, NetworkError, UnauthorizedError
from yandex_music.utils.sign_request import DEFAULT_SIGN_KEY

if TYPE_CHECKING:
    from yandex_music import DownloadInfo
    from yandex_music.feed.feed import Feed
    from yandex_music.landing.chart_info import ChartInfo
    from yandex_music.landing.landing import Landing
    from yandex_music.landing.landing_list import LandingList
    from yandex_music.rotor.dashboard import Dashboard
    from yandex_music.rotor.station_result import StationResult

from .constants import DEFAULT_LIMIT, ROTOR_STATION_MY_WAVE

# get-file-info with quality=lossless returns FLAC; default /tracks/.../download-info often does not
# Prefer flac-mp4/aac-mp4 (Yandex API moved to these formats around 2025)
GET_FILE_INFO_CODECS = "flac-mp4,flac,aac-mp4,aac,he-aac,mp3,he-aac-mp4"

LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


class YandexMusicClient:
    """Wrapper around yandex-music-api ClientAsync."""

    def __init__(self, token: str, base_url: str | None = None) -> None:
        """Initialize the Yandex Music client.

        :param token: Yandex Music OAuth token.
        :param base_url: Optional API base URL (defaults to Yandex Music API).
        """
        self._token = token
        self._base_url = base_url
        self._client: ClientAsync | None = None
        self._user_id: int | None = None
        self._last_reconnect_at: float = -30.0  # allow first reconnect immediately
        self._reconnect_lock = asyncio.Lock()

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
            self._client = await ClientAsync(self._token, base_url=self._base_url).init()
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

    async def _ensure_connected(self) -> ClientAsync:
        """Ensure the client is connected, attempting reconnect if needed."""
        if self._client is not None:
            return self._client
        async with self._reconnect_lock:
            # Re-check after acquiring lock — another task may have connected already
            if self._client is not None:
                return self._client  # type: ignore[unreachable]
            LOGGER.info("Client disconnected, attempting to reconnect...")
            try:
                await self.connect()
            except LoginFailed:
                raise
            except Exception as err:
                raise ProviderUnavailableError("Client not connected and reconnect failed") from err
        return cast("ClientAsync", self._client)

    def _is_connection_error(self, err: Exception) -> bool:
        """Return True if the exception indicates a connection or server drop."""
        if isinstance(err, NetworkError):
            return True
        msg = str(err).lower()
        return "disconnect" in msg or "connection" in msg or "timeout" in msg

    async def _reconnect(self) -> None:
        """Disconnect and connect again to recover from Server disconnected / connection errors.

        Enforces a 30-second cooldown between reconnect attempts to avoid hammering Yandex
        and triggering rate limiting. A lock ensures concurrent callers don't bypass the cooldown.
        """
        async with self._reconnect_lock:
            now = time.monotonic()
            if now - self._last_reconnect_at < 30.0:
                raise ProviderUnavailableError("Reconnect cooldown active, skipping")
            self._last_reconnect_at = now
            await self.disconnect()
            await self.connect()

    async def _call_with_retry(self, func: Callable[[ClientAsync], Awaitable[_T]]) -> _T:
        """Execute an async API call with one reconnect attempt on connection error.

        :param func: Async callable that takes a ClientAsync and returns a result.
        :return: The result of the API call.
        """
        client = await self._ensure_connected()
        try:
            return await func(client)
        except Exception as err:
            if not self._is_connection_error(err):
                raise
            LOGGER.warning("Connection error, reconnecting and retrying: %s", err)
            try:
                await self._reconnect()
            except Exception as recon_err:
                raise ProviderUnavailableError("Reconnect failed") from recon_err
            client = cast("ClientAsync", self._client)
            return await func(client)

    async def _call_no_retry(self, func: Callable[[ClientAsync], Awaitable[_T]]) -> _T:
        """Execute an async API call without reconnect retry on call failure.

        Used for fire-and-forget calls (e.g. rotor feedback) where a failed request
        should be silently dropped rather than triggering a reconnect cycle that
        could cause rate limiting. Note: _ensure_connected() is still called to
        establish the initial connection if needed; only the reconnect-on-error
        path is skipped.

        :param func: Async callable that takes a ClientAsync and returns a result.
        :return: The result of the API call.
        """
        client = await self._ensure_connected()
        return await func(client)

    # Rotor (radio station) methods

    async def get_rotor_station_tracks(
        self,
        station_id: str,
        queue: str | int | None = None,
    ) -> tuple[list[YandexTrack], str | None]:
        """Get tracks from a rotor station (e.g. user:onyourwave or track:1234).

        :param station_id: Station ID (e.g. ROTOR_STATION_MY_WAVE or "track:1234" for similar).
        :param queue: Optional track ID for pagination (first track of previous batch).
        :return: Tuple of (list of track objects, batch_id for feedback or None).
        """
        try:
            result = await self._call_with_retry(
                lambda c: c.rotor_station_tracks(station_id, settings2=True, queue=queue)
            )
        except BadRequestError as err:
            LOGGER.warning("Error fetching rotor station %s tracks: %s", station_id, err)
            return ([], None)
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning("Error fetching rotor station tracks: %s", err)
            return ([], None)

        if not result or not result.sequence:
            return ([], result.batch_id if result else None)
        track_ids = []
        for seq in result.sequence:
            if seq.track is None:
                continue
            tid = getattr(seq.track, "id", None) or getattr(seq.track, "track_id", None)
            if tid is not None:
                track_ids.append(str(tid))
        if not track_ids:
            return ([], result.batch_id if result else None)
        try:
            full_tracks = await self.get_tracks(track_ids)
        except ResourceTemporarilyUnavailable as err:
            LOGGER.warning("Error fetching rotor station track details: %s", err)
            return ([], result.batch_id if result else None)
        order_map = {str(t.id): t for t in full_tracks if hasattr(t, "id") and t.id}
        ordered = [order_map[tid] for tid in track_ids if tid in order_map]
        return (ordered, result.batch_id if result else None)

    async def get_my_wave_tracks(
        self, queue: str | int | None = None
    ) -> tuple[list[YandexTrack], str | None]:
        """Get tracks from the My Wave radio station.

        :param queue: Optional track ID of the last track from the previous batch (API uses it for
            pagination; do not pass batch_id).
        :return: Tuple of (list of track objects, batch_id for feedback).
        """
        return await self.get_rotor_station_tracks(ROTOR_STATION_MY_WAVE, queue=queue)

    async def send_rotor_station_feedback(
        self,
        station_id: str,
        feedback_type: str,
        *,
        batch_id: str | None = None,
        track_id: str | None = None,
        total_played_seconds: int | None = None,
    ) -> bool:
        """Send rotor station feedback for My Wave recommendations.

        Used to report radioStarted, trackStarted, trackFinished, skip so that
        Yandex can improve subsequent recommendations.

        :param station_id: Station ID (e.g. ROTOR_STATION_MY_WAVE).
        :param feedback_type: One of 'radioStarted', 'trackStarted', 'trackFinished', 'skip'.
        :param batch_id: Optional batch ID from the last get_my_wave_tracks response.
        :param track_id: Track ID (required for trackStarted, trackFinished, skip).
        :param total_played_seconds: Seconds played (for trackFinished, skip).
        :return: True if the request succeeded.
        """
        payload: dict[str, Any] = {
            "type": feedback_type,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        if feedback_type == "radioStarted":
            payload["from"] = "YandexMusicDesktopAppWindows"
        if track_id is not None:
            payload["trackId"] = track_id
        if total_played_seconds is not None:
            payload["totalPlayedSeconds"] = total_played_seconds
        if batch_id is not None:
            payload["batchId"] = batch_id

        async def _post(c: ClientAsync) -> bool:
            url = f"{c.base_url}/rotor/station/{station_id}/feedback"
            await c._request.post(url, payload)
            return True

        try:
            result = await self._call_no_retry(_post)
            LOGGER.debug(
                "Rotor feedback %s track_id=%s total_played_seconds=%s",
                feedback_type,
                track_id,
                total_played_seconds,
            )
            return result
        except BadRequestError as err:
            LOGGER.warning("Rotor feedback %s failed: %s", feedback_type, err)
            return False
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning("Rotor feedback %s failed: %s", feedback_type, err)
            return False

    # Library methods

    async def get_liked_tracks(self) -> list[TrackShort]:
        """Get user's liked tracks sorted by timestamp (most recent first).

        :return: List of liked track objects sorted in reverse chronological order.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_tracks())
            if result is None:
                return []
            tracks = result.tracks or []
            # Sort by timestamp in descending order (most recently liked first)
            # TrackShort objects have a timestamp field containing the date the track was liked
            return sorted(
                tracks,
                key=lambda t: getattr(t, "timestamp", datetime.min.replace(tzinfo=UTC)),
                reverse=True,
            )
        except BadRequestError as err:
            LOGGER.error("Error fetching liked tracks: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked tracks") from err
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching liked tracks: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked tracks") from err

    async def get_liked_albums(self, batch_size: int = 50) -> list[YandexAlbum]:
        """Get user's liked albums with full details (including cover art).

        The users_likes_albums endpoint returns minimal album data without
        cover_uri, so we fetch full album details in batches afterwards.

        :return: List of liked album objects with full details.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_albums())
        except BadRequestError as err:
            LOGGER.error("Error fetching liked albums: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked albums") from err
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching liked albums: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked albums") from err

        if result is None:
            return []
        album_ids = [
            str(like.album.id) for like in result if like.album is not None and like.album.id
        ]
        if not album_ids:
            return []
        # Fetch full album details in batches to get cover_uri and other metadata
        full_albums: list[YandexAlbum] = []
        for i in range(0, len(album_ids), batch_size):
            batch = album_ids[i : i + batch_size]
            try:
                batch_result = await self._call_with_retry(
                    lambda c, _b=batch: c.albums(_b)  # type: ignore[misc]
                )
                if batch_result:
                    full_albums.extend(batch_result)
            except (BadRequestError, NetworkError, ProviderUnavailableError) as batch_err:
                LOGGER.warning("Error fetching album details batch: %s", batch_err)
                # Fall back to minimal data for this batch
                batch_set = set(batch)
                for like in result:
                    if like.album is not None and like.album.id and str(like.album.id) in batch_set:
                        full_albums.append(like.album)
        return full_albums

    async def get_liked_artists(self) -> list[YandexArtist]:
        """Get user's liked artists.

        :return: List of liked artist objects.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_artists())
            if result is None:
                return []
            return [like.artist for like in result if like.artist is not None]
        except BadRequestError as err:
            LOGGER.error("Error fetching liked artists: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked artists") from err
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching liked artists: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked artists") from err

    async def get_user_playlists(self) -> list[YandexPlaylist]:
        """Get user's playlists.

        :return: List of playlist objects.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_playlists_list())
            if result is None:
                return []
            return list(result)
        except BadRequestError as err:
            LOGGER.error("Error fetching playlists: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch playlists") from err
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching playlists: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch playlists") from err

    async def get_liked_playlists(self) -> list[YandexPlaylist]:
        """Get user's liked/saved editorial playlists.

        :return: List of liked playlist objects.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_playlists())
            if result is None:
                return []
            playlists = []
            for like in result:
                if like.playlist is not None:
                    playlists.append(like.playlist)
            return playlists
        except BadRequestError as err:
            LOGGER.error("Error fetching liked playlists: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked playlists") from err
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching liked playlists: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked playlists") from err

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
        try:
            return await self._call_with_retry(
                lambda c: c.search(query, type_=search_type, page=0, nocorrect=False)
            )
        except BadRequestError as err:
            LOGGER.error("Search error: %s", err)
            raise ResourceTemporarilyUnavailable("Search failed") from err
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Search error: %s", err)
            raise ResourceTemporarilyUnavailable("Search failed") from err

    # Get single items

    async def get_track(self, track_id: str) -> YandexTrack | None:
        """Get a single track by ID.

        :param track_id: Track ID.
        :return: Track object or None if not found.
        """
        try:
            tracks = await self._call_with_retry(lambda c: c.tracks([track_id]))
            return tracks[0] if tracks else None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching track %s: %s", track_id, err)
            return None

    async def get_track_lyrics(self, track_id: str) -> tuple[str | None, bool]:
        """Get lyrics for a track.

        Fetches lyrics from Yandex Music API. Returns the lyrics text and whether
        it's in synced LRC format (with timestamps) or plain text.

        Note: This method fetches the track first to check lyrics_available. If you
        already have the YandexTrack object, use get_track_lyrics_from_track() to
        avoid a redundant API call.

        :param track_id: Track ID.
        :return: Tuple of (lyrics_text, is_synced). Returns (None, False) if unavailable.
        """
        try:
            tracks = await self._call_with_retry(lambda c: c.tracks([track_id]))
            if not tracks:
                return None, False

            return await self.get_track_lyrics_from_track(tracks[0])

        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching lyrics for track %s: %s", track_id, err)
            return None, False
        except Exception as err:
            # Catch any other errors (e.g., geo-restrictions, API changes)
            LOGGER.debug("Unexpected error fetching lyrics for track %s: %s", track_id, err)
            return None, False

    async def get_track_lyrics_from_track(self, track: YandexTrack) -> tuple[str | None, bool]:
        """Get lyrics for an already-fetched track.

        Avoids the extra tracks([track_id]) API call when the YandexTrack object
        is already available.

        :param track: YandexTrack object (already fetched).
        :return: Tuple of (lyrics_text, is_synced). Returns (None, False) if unavailable.
        """
        track_id = getattr(track, "id", None) or getattr(track, "track_id", "unknown")
        try:
            if not getattr(track, "lyrics_available", False):
                LOGGER.debug("Lyrics not available for track %s", track_id)
                return None, False

            track_lyrics = await track.get_lyrics_async()
            if not track_lyrics:
                LOGGER.debug("Failed to get lyrics metadata for track %s", track_id)
                return None, False

            lyrics_text = await track_lyrics.fetch_lyrics_async()
            if not lyrics_text:
                return None, False

            # Check if it's LRC format (synced lyrics have timestamps like [00:12.34])
            # Use re.search without ^ so metadata lines like [ar:Artist] don't prevent detection
            is_synced = bool(re.search(r"\[\d{2}:\d{2}(?:\.\d{2,3})?\]", lyrics_text))
            return lyrics_text, is_synced

        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching lyrics for track %s: %s", track_id, err)
            return None, False
        except Exception as err:
            # Catch any other errors (e.g., geo-restrictions, API changes)
            LOGGER.debug("Unexpected error fetching lyrics for track %s: %s", track_id, err)
            return None, False

    async def get_tracks(self, track_ids: list[str]) -> list[YandexTrack]:
        """Get multiple tracks by IDs.

        :param track_ids: List of track IDs.
        :return: List of track objects.
        :raises ResourceTemporarilyUnavailable: On network errors after retry.
        """
        try:
            result = await self._call_with_retry(lambda c: c.tracks(track_ids))
            return result or []
        except BadRequestError as err:
            LOGGER.error("Error fetching tracks: %s", err)
            return []
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching tracks (retry failed): %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch tracks") from err

    async def get_album(self, album_id: str) -> YandexAlbum | None:
        """Get a single album by ID.

        :param album_id: Album ID.
        :return: Album object or None if not found.
        """
        try:
            albums = await self._call_with_retry(lambda c: c.albums([album_id]))
            return albums[0] if albums else None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
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

        async def _fetch(c: ClientAsync) -> YandexAlbum | None:
            try:
                return await c.albums_with_tracks(
                    album_id,
                    resumeStream=True,
                    richTracks=True,
                    withListeningFinished=True,
                )
            except TypeError:
                # Older yandex-music may not accept these kwargs
                return await c.albums_with_tracks(album_id)

        try:
            return await self._call_with_retry(_fetch)
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching album with tracks %s: %s", album_id, err)
            return None

    async def get_artist(self, artist_id: str) -> YandexArtist | None:
        """Get a single artist by ID.

        :param artist_id: Artist ID.
        :return: Artist object or None if not found.
        """
        try:
            artists = await self._call_with_retry(lambda c: c.artists([artist_id]))
            return artists[0] if artists else None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
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
        try:
            result = await self._call_with_retry(
                lambda c: c.artists_direct_albums(artist_id, page=0, page_size=limit)
            )
            if result is None:
                return []
            return result.albums or []
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
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
        try:
            result = await self._call_with_retry(
                lambda c: c.artists_tracks(artist_id, page=0, page_size=limit)
            )
            if result is None:
                return []
            return result.tracks or []
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching artist tracks %s: %s", artist_id, err)
            return []

    async def get_playlist(self, user_id: str, playlist_id: str) -> YandexPlaylist | None:
        """Get a playlist by ID.

        :param user_id: User ID (owner of the playlist).
        :param playlist_id: Playlist ID (kind).
        :return: Playlist object or None if not found.
        :raises ResourceTemporarilyUnavailable: On network errors.
        """
        try:
            result = await self._call_with_retry(
                lambda c: c.users_playlists(kind=int(playlist_id), user_id=user_id)
            )
            if isinstance(result, list):
                return result[0] if result else None
            return result
        except BadRequestError as err:
            LOGGER.error("Error fetching playlist %s/%s: %s", user_id, playlist_id, err)
            return None
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning("Network error fetching playlist %s/%s: %s", user_id, playlist_id, err)
            raise ResourceTemporarilyUnavailable("Failed to fetch playlist") from err

    # Streaming

    async def get_track_download_info(
        self, track_id: str, get_direct_links: bool = True
    ) -> list[DownloadInfo]:
        """Get download info for a track.

        :param track_id: Track ID.
        :param get_direct_links: Whether to get direct download links.
        :return: List of download info objects.
        """
        try:
            result = await self._call_with_retry(
                lambda c: c.tracks_download_info(track_id, get_direct_links=get_direct_links)
            )
            return result or []
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching download info for track %s: %s", track_id, err)
            return []

    async def get_track_file_info_lossless(self, track_id: str) -> dict[str, Any] | None:
        """Request lossless stream via get-file-info (quality=lossless).

        The /tracks/{id}/download-info endpoint often returns only MP3; get-file-info
        with quality=lossless and codecs=flac,... returns FLAC when available.

        Uses manual sign calculation matching yandex-music-downloader-realflac.
        Uses _call_with_retry for automatic reconnection on transient failures.

        :param track_id: Track ID.
        :return: Parsed downloadInfo dict (url, codec, urls, ...) or None on error.
        """

        def _build_signed_params(client: ClientAsync) -> tuple[str, dict[str, Any]]:
            """Build URL and signed params using current client and timestamp.

            Called on each attempt by _call_with_retry, so the HMAC signature
            is recomputed with a fresh timestamp on every retry.
            """
            timestamp = int(time.time())
            params = {
                "ts": timestamp,
                "trackId": track_id,
                "quality": "lossless",
                "codecs": GET_FILE_INFO_CODECS,
                "transports": "encraw",
            }
            # Build sign string explicitly matching Yandex API specification:
            # concatenate ts + trackId + quality + codecs (commas stripped) + transports.
            # Comma stripping matches yandex-music-downloader-realflac reference implementation
            # (see get_file_info signing in that project).
            codecs_for_sign = GET_FILE_INFO_CODECS.replace(",", "")
            param_string = f"{timestamp}{track_id}lossless{codecs_for_sign}encraw"
            hmac_sign = hmac.new(
                DEFAULT_SIGN_KEY.encode(),
                param_string.encode(),
                hashlib.sha256,
            )
            # SHA-256 (32 bytes) -> base64 = 44 chars with "=" padding.
            # Yandex API expects exactly 43 chars (one "=" removed).
            # Matches yandex-music-downloader-realflac reference implementation.
            params["sign"] = base64.b64encode(hmac_sign.digest()).decode()[:-1]
            url = f"{client.base_url}/get-file-info"
            return url, params

        def _parse_file_info_result(raw: dict[str, Any] | None) -> dict[str, Any] | None:
            if not raw or not isinstance(raw, dict):
                return None
            download_info = raw.get("download_info")
            if not download_info or not download_info.get("url"):
                return None

            result = cast("dict[str, Any]", download_info)

            if "key" in download_info:
                result["needs_decryption"] = True
                LOGGER.debug(
                    "Encrypted URL received for track %s, will require decryption",
                    track_id,
                )
            else:
                result["needs_decryption"] = False

            return result

        async def _do_request(c: ClientAsync) -> dict[str, Any] | None:
            url, params = _build_signed_params(c)
            return await c._request.get(url, params=params)  # type: ignore[no-any-return]

        try:
            result = await self._call_with_retry(_do_request)
            parsed = _parse_file_info_result(result)
            if parsed:
                LOGGER.debug(
                    "get-file-info lossless for track %s: Success, codec=%s",
                    track_id,
                    parsed.get("codec"),
                )
                return parsed
        except (BadRequestError, NetworkError) as err:
            LOGGER.debug(
                "get-file-info lossless for track %s: %s %s",
                track_id,
                type(err).__name__,
                getattr(err, "message", str(err)) or repr(err),
            )
        except UnauthorizedError as err:
            LOGGER.debug(
                "get-file-info lossless for track %s: UnauthorizedError %s",
                track_id,
                getattr(err, "message", str(err)) or repr(err),
            )
        except Exception as err:
            LOGGER.warning(
                "get-file-info lossless for track %s: Unexpected error: %s",
                track_id,
                err,
                exc_info=True,
            )

        return None

    # Discovery / recommendations

    async def get_feed(self) -> Feed | None:
        """Get personalized feed with generated playlists (Playlist of the Day, etc.).

        :return: Feed object with generated_playlists, or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.feed())
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching feed: %s", err)
            return None

    async def get_chart(self, chart_option: str = "") -> ChartInfo | None:
        """Get chart data.

        :param chart_option: Optional chart variant (e.g. 'world', 'russia').
        :return: ChartInfo object or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.chart(chart_option))
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching chart: %s", err)
            return None

    async def get_new_releases(self) -> LandingList | None:
        """Get new album releases.

        :return: LandingList with new_releases (list of album IDs) or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.new_releases())
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching new releases: %s", err)
            return None

    async def get_new_playlists(self) -> LandingList | None:
        """Get new editorial playlists.

        :return: LandingList with new_playlists (list of PlaylistId) or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.new_playlists())
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching new playlists: %s", err)
            return None

    async def get_albums(self, album_ids: list[str]) -> list[YandexAlbum]:
        """Get multiple albums by IDs.

        :param album_ids: List of album IDs.
        :return: List of album objects.
        """
        try:
            result = await self._call_with_retry(lambda c: c.albums(album_ids))
            return result or []
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching albums: %s", err)
            return []

    async def get_playlists(self, playlist_ids: list[str]) -> list[YandexPlaylist]:
        """Get multiple playlists by IDs (format: 'uid:kind').

        :param playlist_ids: List of playlist IDs in 'uid:kind' format.
        :return: List of playlist objects.
        """
        try:
            result = await self._call_with_retry(lambda c: c.playlists_list(playlist_ids))
            return result or []
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching playlists: %s", err)
            return []

    async def get_tag_playlists(self, tag_id: str) -> list[YandexPlaylist]:
        """Get playlists for a specific tag (mood, era, activity, genre, etc.).

        Tags are used for curated collections like 'chill', '80s', 'workout', 'rock', etc.
        The API returns playlist IDs which are then fetched in full.

        :param tag_id: Tag identifier (e.g. 'chill', '80s', 'workout', 'rock').
        :return: List of playlist objects with full details.
        """
        try:
            tag_result = await self._call_with_retry(lambda c: c.tags(tag_id))
            if not tag_result or not tag_result.ids:
                LOGGER.debug("No playlists found for tag: %s", tag_id)
                return []

            # Convert PlaylistId objects to 'uid:kind' format
            playlist_ids = [f"{pid.uid}:{pid.kind}" for pid in tag_result.ids]

            # Fetch full playlist details
            return await self.get_playlists(playlist_ids)
        except BadRequestError as err:
            LOGGER.debug("Tag %s not found: %s", tag_id, err)
            return []
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching tag %s playlists: %s", tag_id, err)
            return []

    async def get_landing_tags(self) -> list[tuple[str, str]]:
        """Discover available tag slugs from the landing mixes block.

        Uses the landing("mixes") API which returns MixLink entities
        containing tag URLs (e.g., /tag/chill/) and display titles.
        Filters out editorial post entries (/post/ URLs) which have no playlists.

        :return: List of (tag_slug, title) tuples for real tag entries only.
        """
        try:
            landing: Landing | None = await self._call_with_retry(lambda c: c.landing("mixes"))
            if not landing or not landing.blocks:
                return []
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching landing tags: %s", err)
            return []

        tags: list[tuple[str, str]] = []
        for block in landing.blocks:
            if not block.entities:
                continue
            for entity in block.entities:
                if entity.type == "mix-link" and isinstance(entity.data, MixLink):
                    url = entity.data.url  # e.g., "/tag/chill/" or "/post/..."
                    # Filter out editorial posts — only include /tag/ URLs
                    if not url.startswith("/tag/"):
                        continue
                    slug = url.strip("/").split("/")[-1]
                    if slug:
                        tags.append((slug, entity.data.title))
        return tags

    async def get_mixes_waves(self) -> list[dict[str, Any]] | None:
        """Get AI Wave Set stations from /landing-blocks/mixes-waves endpoint.

        Returns structured mix data with categories and station items, each
        containing station_id, title, seeds, and visual metadata.

        :return: List of mix category dicts, or None on error.
        """
        return await self._get_landing_waves("mixes-waves")

    async def get_waves_landing(self) -> list[dict[str, Any]] | None:
        """Get featured wave stations from /landing-blocks/waves endpoint.

        Returns Yandex-curated wave categories with station items — the "Волны"
        landing page content, separate from the full rotor/stations/list and from
        the AI mixes-waves sets.

        :return: List of wave category dicts, or None on error.
        """
        return await self._get_landing_waves("waves")

    async def _get_landing_waves(self, block: str) -> list[dict[str, Any]] | None:
        """Fetch wave categories from a /landing-blocks/<block> endpoint.

        Note: Response keys are auto-converted from camelCase to snake_case
        by the yandex-music library's JSON parser.

        :param block: Block name, e.g. 'waves' or 'mixes-waves'.
        :return: List of wave category dicts, or None on error.
        """

        async def _get(c: ClientAsync) -> dict[str, Any]:
            url = f"{c.base_url}/landing-blocks/{block}"
            return await c._request.get(url)  # type: ignore[no-any-return]

        try:
            result = await self._call_with_retry(_get)
            if result and isinstance(result, dict):
                waves = result.get("waves", [])
                LOGGER.debug(
                    "landing-blocks/%s returned %d categories",
                    block,
                    len(waves) if isinstance(waves, list) else -1,
                )
                return waves if isinstance(waves, list) else []
            return None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching landing-blocks/%s: %s", block, err)
            return None

    async def get_wave_stations(
        self, language: str | None = None
    ) -> list[tuple[str, str, str, str | None]]:
        """Get available rotor wave stations grouped by category.

        Calls rotor_stations_list() — equivalent to the rotor/stations/list API endpoint.
        Filters out personal stations (type 'user') since My Wave is handled separately.

        :param language: Language for station names (e.g. 'ru', 'en'). Defaults to API default.
        :return: List of (station_id, category, name, image_url) tuples,
                 e.g. ('genre:rock', 'genre', 'Рок', 'https://...').
        """
        try:
            results: list[StationResult] = await self._call_with_retry(
                lambda c: c.rotor_stations_list(language)
            )
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning("Error fetching wave stations: %s", err)
            return []

        stations: list[tuple[str, str, str, str | None]] = []
        for result in results or []:
            station = result.station
            if station is None or station.id is None:
                continue
            category = station.id.type
            tag = station.id.tag
            if not category or not tag:
                continue
            if category in ("user", "local-language"):
                # Skip personal stations (My Wave is handled separately)
                # and local-language stations (Yandex returns overlapping tracks across them)
                continue
            station_id = f"{category}:{tag}"
            name = station.name or result.rup_title or tag
            image_url: str | None = None
            raw_url = station.full_image_url or (station.icon.image_url if station.icon else None)
            if raw_url:
                # Yandex avatar URIs use '%%' as a size placeholder; replace it with
                # the desired size. If no placeholder, append the size as a suffix
                # since these URLs return HTTP 400 without a size component.
                if not raw_url.startswith("http"):
                    raw_url = f"https://{raw_url}"
                if "%%" in raw_url:
                    image_url = raw_url.replace("%%", "400x400")
                else:
                    image_url = f"{raw_url}/400x400"
            stations.append((station_id, category, name, image_url))
        return stations

    async def get_dashboard_stations(self) -> list[tuple[str, str, str | None]]:
        """Get personalized recommended stations for the current user.

        Calls rotor_stations_dashboard() — returns user-specific stations based
        on listening history, unlike rotor_stations_list() which is non-personalized.

        :return: List of (station_id, name, image_url) tuples,
                 e.g. ('genre:rock', 'Рок', 'https://...').
        """
        try:
            dashboard: Dashboard | None = await self._call_with_retry(
                lambda c: c.rotor_stations_dashboard()
            )
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning("Error fetching dashboard stations: %s", err)
            return []

        if not dashboard or not dashboard.stations:
            return []

        stations: list[tuple[str, str, str | None]] = []
        for result in dashboard.stations:
            station = result.station
            if station is None or station.id is None:
                continue
            category = station.id.type
            tag = station.id.tag
            if not category or not tag:
                continue
            if category == "user":
                continue
            station_id = f"{category}:{tag}"
            name = station.name or result.rup_title or tag
            image_url: str | None = None
            raw_url = station.full_image_url or (station.icon.image_url if station.icon else None)
            if raw_url:
                if not raw_url.startswith("http"):
                    raw_url = f"https://{raw_url}"
                if "%%" in raw_url:
                    image_url = raw_url.replace("%%", "400x400")
                else:
                    image_url = f"{raw_url}/400x400"
            stations.append((station_id, name, image_url))
        return stations

    # Library modifications

    async def like_track(self, track_id: str) -> bool:
        """Add a track to liked tracks.

        :param track_id: Track ID to like.
        :return: True if successful.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_tracks_add(track_id))
            return result is not None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error liking track %s: %s", track_id, err)
            return False

    async def unlike_track(self, track_id: str) -> bool:
        """Remove a track from liked tracks.

        :param track_id: Track ID to unlike.
        :return: True if successful.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_tracks_remove(track_id))
            return result is not None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error unliking track %s: %s", track_id, err)
            return False

    async def like_album(self, album_id: str) -> bool:
        """Add an album to liked albums.

        :param album_id: Album ID to like.
        :return: True if successful.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_albums_add(album_id))
            return result is not None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error liking album %s: %s", album_id, err)
            return False

    async def unlike_album(self, album_id: str) -> bool:
        """Remove an album from liked albums.

        :param album_id: Album ID to unlike.
        :return: True if successful.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_albums_remove(album_id))
            return result is not None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error unliking album %s: %s", album_id, err)
            return False

    async def like_artist(self, artist_id: str) -> bool:
        """Add an artist to liked artists.

        :param artist_id: Artist ID to like.
        :return: True if successful.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_artists_add(artist_id))
            return result is not None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error liking artist %s: %s", artist_id, err)
            return False

    async def unlike_artist(self, artist_id: str) -> bool:
        """Remove an artist from liked artists.

        :param artist_id: Artist ID to unlike.
        :return: True if successful.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_artists_remove(artist_id))
            return result is not None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error unliking artist %s: %s", artist_id, err)
            return False
