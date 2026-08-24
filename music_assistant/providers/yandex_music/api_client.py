"""API client wrapper for Yandex Music."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import random
import re
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar, cast

from music_assistant_models.errors import (
    LoginFailed,
    ProviderUnavailableError,
    RateLimited,
    ResourceTemporarilyUnavailable,
)
from yandex_music import Album as YandexAlbum
from yandex_music import Artist as YandexArtist
from yandex_music import ClientAsync, MixLink, Search, TrackShort
from yandex_music import Playlist as YandexPlaylist
from yandex_music import Track as YandexTrack
from yandex_music.exceptions import BadRequestError, NetworkError, UnauthorizedError
from yandex_music.utils.sign_request import DEFAULT_SIGN_KEY

from music_assistant.helpers.datetime import utc
from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER, Throttler

if TYPE_CHECKING:
    from ya_passport_auth import SecretStr
    from yandex_music import DownloadInfo
    from yandex_music.feed.feed import Feed
    from yandex_music.landing.chart_info import ChartInfo
    from yandex_music.landing.landing import Landing
    from yandex_music.landing.landing_list import LandingList
    from yandex_music.rotor.dashboard import Dashboard
    from yandex_music.rotor.station_result import StationResult

from .constants import (
    CAPTCHA_COOLDOWN_LADDER_S,
    CAPTCHA_STRIKE_RETENTION_S,
    DEFAULT_LIMIT,
    FILE_INFO_CACHE_MAX,
    FILE_INFO_CACHE_TTL_S,
    INITIAL_SYNC_JITTER_S,
    INITIAL_SYNC_WINDOW_S,
    LIKED_BATCH_JITTER_MIN_S,
    LIKED_BATCH_JITTER_SPAN_S,
    RATE_LIMIT_COOLDOWN_S,
    RESTRICTIVE_GLOBAL_CONCURRENCY,
    THROTTLE_DEFAULT_RPS,
    THROTTLE_FILE_INFO_RPS,
    THROTTLE_METADATA_RPS,
    THROTTLE_ROTOR_RPS,
)

_CAPTCHA_MARKERS: Final = ("smart-captcha", "captcha_smart_qrcode", "about-429.html")

# get-file-info with quality=lossless returns FLAC; default /tracks/.../download-info often does not
# Prefer flac-mp4/aac-mp4 (Yandex API moved to these formats around 2025)
GET_FILE_INFO_CODECS = "flac-mp4,flac,aac-mp4,aac,he-aac,mp3,he-aac-mp4"

LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


def _liked_track_sort_key(track: Any) -> datetime:
    """
    Return a naive ``datetime`` for sorting liked tracks chronologically.

    Yandex's ``TrackShort.timestamp`` is sometimes tz-aware and sometimes
    tz-naive depending on the upstream library version; mixing the two
    triggers ``TypeError`` in ``sorted``. Strip ``tzinfo`` and fall back to
    ``datetime.min`` when the field is missing.
    """
    ts = getattr(track, "timestamp", None)
    if not isinstance(ts, datetime):
        return datetime.min  # noqa: DTZ901 — naive sentinel by design (see docstring)
    if ts.tzinfo is not None:
        return ts.replace(tzinfo=None)
    return ts


class YandexMusicClient:
    """Wrapper around yandex-music-api ClientAsync."""

    def __init__(
        self,
        token: SecretStr,
        base_url: str | None = None,
        *,
        restrictive_rate_limits: bool = False,
    ) -> None:
        """
        Initialize the Yandex Music client.

        :param token: Yandex Music OAuth token (wrapped in SecretStr).
        :param base_url: Optional API base URL (defaults to Yandex Music API).
        :param restrictive_rate_limits: When True, applies a token-wide
            concurrency cap (``RESTRICTIVE_GLOBAL_CONCURRENCY``) on top of
            the per-kind throttler and per-endpoint lock — for users on
            VPS / datacenter / VPN IPs where Yandex's edge enforces a
            tighter anti-scraper concurrency limit.
        """
        self._token = token
        self._base_url = base_url
        self._client: ClientAsync | None = None
        self._user_id: int | None = None
        self._last_reconnect_at: float = -30.0  # allow first reconnect immediately
        self._reconnect_lock = asyncio.Lock()
        # Per-kind throttlers. Yandex's smart-captcha quota is per-endpoint-family,
        # so we keep a separate token bucket per logical class and let one kind
        # back off independently of the others. `metadata` covers the artist/album
        # refresh burst MA fires during initial sync (see #146).
        self._throttlers: dict[str, Throttler] = {
            "default": Throttler(rate_limit=THROTTLE_DEFAULT_RPS, period=1.0),
            "metadata": Throttler(rate_limit=THROTTLE_METADATA_RPS, period=1.0),
            "file_info": Throttler(rate_limit=THROTTLE_FILE_INFO_RPS, period=1.0),
            "rotor": Throttler(rate_limit=THROTTLE_ROTOR_RPS, period=1.0),
        }
        # Per-kind captcha quarantine deadlines (monotonic). Only the explicit
        # smart-captcha page sets a deadline; plain 429 leaves these at 0.
        self._block_until: dict[str, float] = dict.fromkeys(self._throttlers, 0.0)
        # Per-kind captcha strike timestamps (monotonic), trimmed to the
        # CAPTCHA_STRIKE_RETENTION_S window on every push. Drives the
        # CAPTCHA_COOLDOWN_LADDER_S escalation.
        self._captcha_strikes: dict[str, deque[float]] = defaultdict(deque)
        # Set when connect() succeeds. Drives the initial-sync jitter window.
        self._connected_at: float | None = None
        # Short-TTL cache for /get-file-info results, keyed by
        # (track_id, quality, codecs, transport). Bounded by FILE_INFO_CACHE_MAX (LRU).
        self._file_info_cache: OrderedDict[
            tuple[str, str, str, str], tuple[float, dict[str, Any]]
        ] = OrderedDict()
        # Per-endpoint concurrency locks. Yandex's edge layer reacts to
        # concurrent requests to the same URL family (per-endpoint scraper
        # signature), not steady-state RPS. Defense-in-depth on top of the
        # per-kind throttler: even if a caller fans out via
        # ``asyncio.gather`` over the same method, the lock serialises the
        # actual HTTP requests to ≤1 concurrent per endpoint. Created lazily
        # on first use to keep the dict small. Lifetime tied to the client
        # instance (rebuilt on reconnect / token rotation).
        self._endpoint_locks: dict[str, asyncio.Lock] = {}
        # Restrictive mode: optional global token-wide concurrency cap.
        # When set, every call through ``_call_with_retry`` must acquire
        # this semaphore before firing — so the total in-flight count
        # across all kinds and endpoints can never exceed
        # ``RESTRICTIVE_GLOBAL_CONCURRENCY``. Lives at the client level
        # because Yandex's edge enforces the cap per-token, not per-kind.
        self._global_concurrency: asyncio.Semaphore | None = (
            asyncio.Semaphore(RESTRICTIVE_GLOBAL_CONCURRENCY) if restrictive_rate_limits else None
        )

    @property
    def user_id(self) -> int:
        """Return the user ID."""
        if self._user_id is None:
            raise ProviderUnavailableError("Client not initialized, call connect() first")
        return self._user_id

    async def connect(self) -> bool:
        """
        Initialize the client and verify token validity.

        :return: True if connection was successful.
        :raises LoginFailed: If the token is invalid.
        """
        try:
            self._client = await ClientAsync(
                self._token.get_secret(), base_url=self._base_url
            ).init()
            if self._client.me is None or self._client.me.account is None:
                raise LoginFailed("Failed to get account info")
            self._user_id = self._client.me.account.uid
            self._connected_at = time.monotonic()
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
        self._connected_at = None

    # Rotor (radio station) methods

    async def get_rotor_station_tracks(
        self,
        station_id: str,
        queue: str | int | None = None,
    ) -> tuple[list[YandexTrack], str | None]:
        """
        Get tracks from a rotor station (e.g. user:onyourwave or track:1234).

        :param station_id: Station ID (e.g. ROTOR_STATION_MY_WAVE or "track:1234" for similar).
        :param queue: Optional track ID for pagination (first track of previous batch).
        :return: Tuple of (list of track objects, batch_id for feedback or None).
        """
        try:
            result = await self._call_with_retry(
                lambda c: c.rotor_station_tracks(station_id, settings2=True, queue=queue),
                kind="rotor",
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

    async def send_rotor_station_feedback(
        self,
        station_id: str,
        feedback_type: str,
        *,
        batch_id: str | None = None,
        track_id: str | None = None,
        total_played_seconds: int | None = None,
    ) -> bool:
        """
        Send rotor station feedback for My Wave recommendations.

        Used to report radioStarted, trackStarted, trackFinished, skip so that
        Yandex can improve subsequent recommendations.

        :param station_id: Station ID (e.g. ROTOR_STATION_MY_WAVE).
        :param feedback_type: One of 'radioStarted', 'trackStarted', 'trackFinished', 'skip'.
        :param batch_id: Optional batch ID from the last get_my_wave_tracks response.
        :param track_id: Track ID (required for trackStarted, trackFinished, skip).
        :param total_played_seconds: Seconds played (for trackFinished, skip).
        :return: True if the request succeeded.
        """
        timestamp = utc().isoformat().replace("+00:00", "Z")

        async def _send(c: ClientAsync) -> bool:
            if feedback_type == "radioStarted":
                return bool(
                    await c.rotor_station_feedback_radio_started(
                        station_id,
                        from_="YandexMusicDesktopAppWindows",
                        batch_id=batch_id,
                        timestamp=timestamp,
                    )
                )
            if feedback_type == "trackStarted":
                if track_id is None:
                    return False
                return bool(
                    await c.rotor_station_feedback_track_started(
                        station_id,
                        track_id=track_id,
                        batch_id=batch_id,
                        timestamp=timestamp,
                    )
                )
            if feedback_type == "trackFinished":
                if track_id is None:
                    return False
                return bool(
                    await c.rotor_station_feedback_track_finished(
                        station_id,
                        track_id=track_id,
                        total_played_seconds=float(total_played_seconds or 0),
                        batch_id=batch_id,
                        timestamp=timestamp,
                    )
                )
            if feedback_type == "skip":
                if track_id is None:
                    return False
                return bool(
                    await c.rotor_station_feedback_skip(
                        station_id,
                        track_id=track_id,
                        total_played_seconds=float(total_played_seconds or 0),
                        batch_id=batch_id,
                        timestamp=timestamp,
                    )
                )
            return bool(
                await c.rotor_station_feedback(
                    station_id,
                    type_=feedback_type,
                    timestamp=timestamp,
                    track_id=track_id,
                    total_played_seconds=total_played_seconds,
                    batch_id=batch_id,
                )
            )

        try:
            result = await self._call_no_retry(_send, kind="rotor")
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
        except ResourceTemporarilyUnavailable as err:
            # 429/captcha already truncated + block engaged inside _call_no_retry.
            LOGGER.warning("Rotor feedback %s rate-limited: %s", feedback_type, err)
            return False
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning(
                "Rotor feedback %s failed: %s",
                feedback_type,
                self._truncate_err_msg(err),
            )
            return False

    async def rotor_session_new(
        self,
        station_id: str,
        *,
        settings: dict[str, str] | None = None,
        queue: list[str] | None = None,
    ) -> tuple[str | None, list[YandexTrack], str | None]:
        """
        Create a new rotor session.

        Sends `includeWaveModel: true` so Yandex applies its wave ML model and
        `interactive: true` so the session is treated as foreground user play.

        :param station_id: Station ID (e.g. "user:onyourwave" or "track:123").
        :param settings: Optional {diversity, moodEnergy, language} — each
            becomes an additional seed like "settingDiversity:discover".
        :param queue: Optional initial track IDs in the queue; usually empty.
        :return: Tuple of (radio_session_id, list of tracks, batch_id).
            Any element may be None/[] on failure.
        """
        seeds: list[str] = [station_id]
        if settings:
            for key, seed_name in (
                ("diversity", "settingDiversity"),
                ("moodEnergy", "settingMoodEnergy"),
                ("language", "settingLanguage"),
            ):
                val = settings.get(key)
                if val:
                    seeds.append(f"{seed_name}:{val}")
        body: dict[str, Any] = {
            "seeds": seeds,
            "queue": queue or [],
            "includeTracksInResponse": True,
            "includeWaveModel": True,
            "interactive": True,
        }
        result = await self._rotor_session_request("new", body)
        if not result:
            return (None, [], None)
        session_id = result.get("radioSessionId")
        batch_id = result.get("batchId")
        tracks = await self._hydrate_session_tracks(result.get("sequence") or [])
        return (session_id, tracks, batch_id)

    async def rotor_session_tracks(
        self, session_id: str, *, current_track_id: str
    ) -> tuple[list[YandexTrack], str | None]:
        """
        Fetch the next batch of tracks for an active rotor session.

        :param session_id: radioSessionId from rotor_session_new().
        :param current_track_id: Track ID just consumed from the previous batch
            (Yandex uses it to decide what to return next).
        :return: Tuple of (list of tracks, new batch_id).
        """
        body = {"queue": [str(current_track_id)]}
        result = await self._rotor_session_request(f"{session_id}/tracks", body)
        if not result:
            return ([], None)
        batch_id = result.get("batchId")
        tracks = await self._hydrate_session_tracks(result.get("sequence") or [])
        return (tracks, batch_id)

    async def rotor_session_feedback(
        self,
        session_id: str,
        event_type: str,
        *,
        track_id: str | None = None,
        total_played_seconds: int | None = None,
        batch_id: str | None = None,
    ) -> bool:
        """
        Send a feedback event for an active rotor session.

        Supports the Yandex rotor event types: radioStarted, trackStarted,
        trackFinished, skip, like, dislike. For radioStarted the track_id goes
        into `event.from`; all other types use `event.trackId`. Only
        trackFinished and skip carry `totalPlayedSeconds`.

        :param session_id: radioSessionId.
        :param event_type: rotor event type string.
        :param track_id: Yandex track ID the event refers to (required for
            everything except radioStarted without a seed).
        :param total_played_seconds: seconds of the track that were played
            (only meaningful for trackFinished / skip).
        :param batch_id: batchId from the most recent rotor_session_{new,tracks}
            response; anchors the event to a specific batch.
        :return: True if the POST succeeded.
        """
        timestamp = utc().isoformat().replace("+00:00", "Z")
        event: dict[str, Any] = {"type": event_type, "timestamp": timestamp}
        if event_type == "radioStarted":
            if track_id is not None:
                event["from"] = str(track_id)
        elif track_id is not None:
            event["trackId"] = str(track_id)
        if event_type in ("trackFinished", "skip") and total_played_seconds is not None:
            event["totalPlayedSeconds"] = int(total_played_seconds)
        body: dict[str, Any] = {"event": event}
        if batch_id:
            body["batchId"] = batch_id
        LOGGER.debug(
            "Rotor session feedback: session=%s event=%s track=%s secs=%s batch=%s",
            session_id,
            event_type,
            track_id,
            total_played_seconds,
            batch_id,
        )
        result = await self._rotor_session_request(f"{session_id}/feedback", body, with_retry=False)
        return result is not None

    async def play_audio(
        self,
        *,
        track_id: str,
        album_id: str,
        play_id: str,
        track_length_seconds: int,
        total_played_seconds: int,
        end_position_seconds: int,
        from_: str = "music_assistant-audiobook",
    ) -> bool:
        """
        Report playback progress for an audiobook chapter or podcast episode.

        Yandex persists this server-side so progress is visible across its
        other clients. Failures are swallowed — progress sync is advisory and
        must never abort pause/stop handling — so auth failures, rate-limits
        and network blips all log at debug and return False.
        """
        try:
            return bool(
                await self._call_no_retry(
                    lambda c: c.play_audio(
                        track_id=track_id,
                        album_id=album_id,
                        from_=from_,
                        play_id=play_id,
                        track_length_seconds=track_length_seconds,
                        total_played_seconds=total_played_seconds,
                        end_position_seconds=end_position_seconds,
                    )
                )
            )
        except (
            BadRequestError,
            NetworkError,
            ProviderUnavailableError,
            UnauthorizedError,
            LoginFailed,
            ResourceTemporarilyUnavailable,
        ) as err:
            LOGGER.debug("play_audio failed for %s: %s", track_id, err)
            return False

    # Library methods

    async def get_liked_tracks(self) -> list[TrackShort]:
        """
        Get user's liked tracks sorted by timestamp (most recent first).

        :return: List of liked track objects sorted in reverse chronological order.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_tracks())
            if result is None:
                return []
            tracks = result.tracks or []
            # Sort by timestamp in descending order (most recently liked first).
            # ``TrackShort.timestamp`` is sometimes tz-aware and sometimes
            # tz-naive depending on the upstream library version, so we
            # normalise to naive before comparing.
            return sorted(tracks, key=_liked_track_sort_key, reverse=True)
        except BadRequestError as err:
            # 4xx is terminal — do not signal retry. MA would otherwise loop.
            LOGGER.warning("Liked tracks unavailable (4xx): %s", err)
            return []
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning("Error fetching liked tracks: %s", err)
            raise ResourceTemporarilyUnavailable("Failed to fetch liked tracks") from err

    async def get_liked_albums(self, batch_size: int = 50) -> list[YandexAlbum]:
        """
        Get user's liked albums with full details (including cover art).

        The users_likes_albums endpoint returns minimal album data without
        cover_uri, so we fetch full album details in batches afterwards.

        :return: List of liked album objects with full details.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_albums())
        except BadRequestError as err:
            LOGGER.warning("Liked albums unavailable (4xx): %s", err)
            return []
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning("Error fetching liked albums: %s", err)
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
            # Spread bursts: small jittered pause before next batch.
            if i + batch_size < len(album_ids):
                await asyncio.sleep(
                    LIKED_BATCH_JITTER_MIN_S + random.random() * LIKED_BATCH_JITTER_SPAN_S
                )
        return full_albums

    async def get_liked_artists(self) -> list[YandexArtist]:
        """
        Get user's liked artists.

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
        """
        Get user's playlists.

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
        """
        Get user's liked/saved editorial playlists.

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
    ) -> Search | None:
        """
        Search for tracks, albums, artists, or playlists.

        The upstream ``yandex-music`` client does not accept a per-type result
        cap at this layer — callers slice the parsed buckets to whatever
        ``limit`` they need after classification.

        :param query: Search query string.
        :param search_type: Type of search ('all', 'track', 'album', 'artist', 'playlist').
        :return: Search results object.
        """
        try:
            return await self._call_with_retry(
                lambda c: c.search(query, type_=search_type, page=0, nocorrect=False)
            )
        except BadRequestError as err:
            # 4xx is terminal (malformed query, geo-block) — return None so MA
            # surfaces "no results" instead of retrying the same failure.
            LOGGER.warning("Search rejected by Yandex (4xx): %s", err)
            return None
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning("Search error: %s", err)
            raise ResourceTemporarilyUnavailable("Search failed") from err

    # Get single items

    async def get_track(self, track_id: str) -> YandexTrack | None:
        """
        Get a single track by ID.

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
        """
        Get lyrics for a track.

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
        """
        Get lyrics for an already-fetched track.

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
        """
        Get multiple tracks by IDs.

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
        """
        Get a single album by ID.

        :param album_id: Album ID.
        :return: Album object or None if not found.
        """
        try:
            albums = await self._call_with_retry(lambda c: c.albums([album_id]), kind="metadata")
            return albums[0] if albums else None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching album %s: %s", album_id, err)
            return None

    async def get_album_with_tracks(self, album_id: str) -> YandexAlbum | None:
        """
        Get an album with its tracks.

        Uses the same semantics as the web client: albums/{id}/with-tracks
        with resumeStream, richTracks, withListeningFinished.

        :param album_id: Album ID.
        :return: Album object with tracks or None if not found.
        """
        try:
            return await self._call_with_retry(
                lambda c: c.albums_with_tracks(
                    album_id,
                    params={
                        "resumeStream": "true",
                        "richTracks": "true",
                        "withListeningFinished": "true",
                    },
                ),
                kind="metadata",
            )
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching album with tracks %s: %s", album_id, err)
            return None

    async def get_artist(self, artist_id: str) -> YandexArtist | None:
        """
        Get a single artist by ID.

        :param artist_id: Artist ID.
        :return: Artist object or None if not found.
        """
        try:
            artists = await self._call_with_retry(lambda c: c.artists([artist_id]), kind="metadata")
            return artists[0] if artists else None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching artist %s: %s", artist_id, err)
            return None

    async def get_artist_albums(
        self, artist_id: str, limit: int = DEFAULT_LIMIT
    ) -> list[YandexAlbum]:
        """
        Get artist's albums.

        :param artist_id: Artist ID.
        :param limit: Maximum number of albums.
        :return: List of album objects.
        """
        try:
            result = await self._call_with_retry(
                lambda c: c.artists_direct_albums(artist_id, page=0, page_size=limit),
                kind="metadata",
            )
            if result is None:
                return []
            return result.albums or []
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching artist albums %s: %s", artist_id, err)
            return []

    async def get_pins(self) -> Any | None:
        """
        Get the user's pinned items (artists/albums/playlists/waves).

        :return: PinsList object or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.pins())
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching pins: %s", err)
            return None

    async def get_music_history(self) -> Any | None:
        """
        Get the user's listening history (grouped by day).

        :return: MusicHistory object or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.music_history())
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching music history: %s", err)
            return None

    async def get_artist_about(self, artist_id: str) -> Any | None:
        """
        Get artist enrichment info: description, monthly listeners, links.

        :param artist_id: Artist ID.
        :return: ArtistAbout object or None on error/missing.
        """
        try:
            return await self._call_with_retry(
                lambda c: c.artists_about(artist_id), kind="metadata"
            )
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching artist about %s: %s", artist_id, err)
            return None

    async def get_similar_artists(
        self, artist_id: str, limit: int = DEFAULT_LIMIT
    ) -> list[YandexArtist]:
        """
        Get artists similar to the given one.

        :param artist_id: Artist ID.
        :param limit: Maximum number of artists.
        :return: List of similar artist objects.
        """
        try:
            result = await self._call_with_retry(lambda c: c.artists_similar(artist_id))
            if result is None or not result.similar_artists:
                return []
            similar: list[YandexArtist] = result.similar_artists
            return similar[:limit]
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching similar artists %s: %s", artist_id, err)
            return []

    async def get_artist_tracks(
        self, artist_id: str, limit: int = DEFAULT_LIMIT
    ) -> list[YandexTrack]:
        """
        Get artist's top tracks.

        :param artist_id: Artist ID.
        :param limit: Maximum number of tracks.
        :return: List of track objects.
        """
        try:
            result = await self._call_with_retry(
                lambda c: c.artists_tracks(artist_id, page=0, page_size=limit),
                kind="metadata",
            )
            if result is None:
                return []
            return result.tracks or []
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error fetching artist tracks %s: %s", artist_id, err)
            return []

    async def get_playlist(self, user_id: str, playlist_id: str) -> YandexPlaylist | None:
        """
        Get a playlist by ID.

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
        """
        Get download info for a track.

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

    async def get_track_file_info(  # noqa: PLR0915
        self,
        track_id: str,
        quality: str = "lossless",
        codecs: str = GET_FILE_INFO_CODECS,
        transport: str = "raw",
    ) -> dict[str, Any] | None:
        """
        Request stream via get-file-info for any quality tier.

        The /get-file-info endpoint supports all quality tiers (lossless, nq, lq)
        and returns the best available codec based on the codecs parameter order.

        With transport="raw", returns a direct unencrypted URL.
        With transport="encraw", returns an AES-CTR encrypted URL with decryption key.

        Uses _call_with_retry for automatic reconnection on transient failures.

        :param track_id: Track ID.
        :param quality: Quality tier ("lossless", "nq", "lq").
        :param codecs: Comma-separated codec preference list.
        :param transport: Transport mode ("raw" or "encraw").
        :return: Parsed downloadInfo dict (url, codec, key?, ...) or None on error.
        """
        # Normalize codecs: strip whitespace from each token to prevent HMAC mismatches
        codecs = ",".join(c.strip() for c in codecs.split(",") if c.strip())

        # Short-TTL cache to absorb repeat calls from MA's streaming retry loop.
        # Bypass when refresh is in progress (BYPASS_THROTTLER): a refresh fires
        # specifically because the previous URL expired on the CDN side, so the
        # cached entry is useless.
        # Include `codecs` in the key: the server may pick a different codec
        # (and URL) based on the codec preference order, so two calls with the
        # same (track, quality, transport) but different codec lists must not
        # share a cache slot.
        cache_key = (track_id, quality, codecs, transport)
        if not BYPASS_THROTTLER.get():
            # Check the file_info circuit-breaker BEFORE the cache lookup —
            # otherwise a cooldown-period caller could be served a stale URL
            # from before the block was engaged. Fail fast (return None) so
            # MA's streaming layer treats the track as unavailable.
            try:
                self._check_block("file_info")
            except ResourceTemporarilyUnavailable as err:
                LOGGER.debug(
                    "get-file-info for track %s: file_info cooldown active (%s)",
                    track_id,
                    err,
                )
                return None
            cached = self._file_info_cache_get(cache_key)
            if cached is not None:
                LOGGER.debug(
                    "get-file-info for track %s: cache hit (transport=%s)",
                    track_id,
                    transport,
                )
                return cached

        def _build_signed_params(client: ClientAsync) -> tuple[str, dict[str, Any]]:
            """
            Build URL and signed params using current client and timestamp.

            Called on each attempt by _call_with_retry, so the HMAC signature
            is recomputed with a fresh timestamp on every retry.
            """
            timestamp = int(time.time())
            params = {
                "ts": timestamp,
                "trackId": track_id,
                "quality": quality,
                "codecs": codecs,
                "transports": transport,
            }
            # Build sign string: ts + trackId + quality + codecs (commas stripped) + transports.
            codecs_for_sign = codecs.replace(",", "")
            param_string = f"{timestamp}{track_id}{quality}{codecs_for_sign}{transport}"
            hmac_sign = hmac.new(
                DEFAULT_SIGN_KEY.encode(),
                param_string.encode(),
                hashlib.sha256,
            )
            # SHA-256 (32 bytes) -> base64 = 44 chars with "=" padding.
            # Yandex API expects exactly 43 chars (one "=" removed).
            params["sign"] = base64.b64encode(hmac_sign.digest()).decode()[:-1]
            url = f"{client.base_url}/get-file-info"
            return url, params

        def _parse_file_info_result(raw: dict[str, Any] | None) -> dict[str, Any] | None:
            if not raw or not isinstance(raw, dict):
                return None
            # yandex-music v3 no longer normalises camelCase keys inside
            # Response.result, so /get-file-info returns "downloadInfo" as-is.
            download_info = raw.get("download_info") or raw.get("downloadInfo")
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
            result = await self._call_with_retry(_do_request, kind="file_info")
            parsed = _parse_file_info_result(result)
            if parsed:
                LOGGER.debug(
                    "get-file-info for track %s: Success, codec=%s, transport=%s",
                    track_id,
                    parsed.get("codec"),
                    transport,
                )
                # Always store the freshest URL — including under BYPASS_THROTTLER.
                # A successful refresh proves the previously cached entry was
                # stale, so replacing it avoids serving the old URL to the next
                # non-bypass caller until its TTL expires.
                self._file_info_cache_put(cache_key, parsed)
                return parsed
        except BadRequestError as err:
            # 4xx is terminal for this URL/quality. Drop any cached entry so we
            # don't replay a now-rejected response.
            self._file_info_cache_invalidate(track_id)
            LOGGER.debug(
                "get-file-info for track %s: BadRequestError %s",
                track_id,
                getattr(err, "message", str(err)) or repr(err),
            )
        except (
            NetworkError,
            ProviderUnavailableError,
            ResourceTemporarilyUnavailable,
        ) as err:
            LOGGER.debug(
                "get-file-info for track %s: %s %s",
                track_id,
                type(err).__name__,
                getattr(err, "message", str(err)) or repr(err),
            )
        except UnauthorizedError as err:
            # Auth expired — invalidate any cached URL so the post-re-auth call
            # doesn't replay a stale entry tied to the old session.
            self._file_info_cache_invalidate(track_id)
            LOGGER.debug(
                "get-file-info for track %s: UnauthorizedError %s",
                track_id,
                getattr(err, "message", str(err)) or repr(err),
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.warning(
                "get-file-info for track %s: Unexpected %s: %s",
                track_id,
                type(err).__name__,
                err,
            )

        return None

    # Discovery / recommendations

    async def get_feed(self) -> Feed | None:
        """
        Get personalized feed with generated playlists (Playlist of the Day, etc.).

        :return: Feed object with generated_playlists, or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.feed())
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching feed: %s", err)
            return None

    async def get_chart(self, chart_option: str = "") -> ChartInfo | None:
        """
        Get chart data.

        :param chart_option: Optional chart variant (e.g. 'world', 'russia').
        :return: ChartInfo object or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.chart(chart_option))
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching chart: %s", err)
            return None

    async def get_new_releases(self) -> LandingList | None:
        """
        Get new album releases.

        :return: LandingList with new_releases (list of album IDs) or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.new_releases())
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching new releases: %s", err)
            return None

    async def get_new_playlists(self) -> LandingList | None:
        """
        Get new editorial playlists.

        :return: LandingList with new_playlists (list of PlaylistId) or None on error.
        """
        try:
            return await self._call_with_retry(lambda c: c.new_playlists())
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.debug("Error fetching new playlists: %s", err)
            return None

    async def get_albums(self, album_ids: list[str]) -> list[YandexAlbum]:
        """
        Get multiple albums by IDs.

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
        """
        Get multiple playlists by IDs (format: 'uid:kind').

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
        """
        Get playlists for a specific tag (mood, era, activity, genre, etc.).

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
        """
        Discover available tag slugs from the landing mixes block.

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
        """
        Get AI Wave Set stations from /landing-blocks/mixes-waves endpoint.

        Returns structured mix data with categories and station items, each
        containing station_id, title, seeds, and visual metadata.

        :return: List of mix category dicts, or None on error.
        """
        return await self._get_landing_waves("mixes-waves")

    async def get_waves_landing(self) -> list[dict[str, Any]] | None:
        """
        Get featured wave stations from /landing-blocks/waves endpoint.

        Returns Yandex-curated wave categories with station items — the "Волны"
        landing page content, separate from the full rotor/stations/list and from
        the AI mixes-waves sets.

        :return: List of wave category dicts, or None on error.
        """
        return await self._get_landing_waves("waves")

    async def get_wave_stations(
        self, language: str | None = None
    ) -> list[tuple[str, str, str, str | None]]:
        """
        Get available rotor wave stations grouped by category.

        Calls rotor_stations_list() — equivalent to the rotor/stations/list API endpoint.
        Filters out personal stations (type 'user') since My Wave is handled separately.

        :param language: Language for station names (e.g. 'ru', 'en'). Defaults to API default.
        :return: List of (station_id, category, name, image_url) tuples,
                 e.g. ('genre:rock', 'genre', 'Рок', 'https://...').
        """
        try:
            results: list[StationResult] = await self._call_with_retry(
                lambda c: c.rotor_stations_list(language),
                kind="rotor",
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
        """
        Get personalized recommended stations for the current user.

        Calls rotor_stations_dashboard() — returns user-specific stations based
        on listening history, unlike rotor_stations_list() which is non-personalized.

        :return: List of (station_id, name, image_url) tuples,
                 e.g. ('genre:rock', 'Рок', 'https://...').
        """
        try:
            dashboard: Dashboard | None = await self._call_with_retry(
                lambda c: c.rotor_stations_dashboard(),
                kind="rotor",
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
        """
        Add a track to liked tracks.

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
        """
        Remove a track from liked tracks.

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
        """
        Add an album to liked albums.

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
        """
        Remove an album from liked albums.

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
        """
        Add an artist to liked artists.

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
        """
        Remove an artist from liked artists.

        :param artist_id: Artist ID to unlike.
        :return: True if successful.
        """
        try:
            result = await self._call_with_retry(lambda c: c.users_likes_artists_remove(artist_id))
            return result is not None
        except (BadRequestError, NetworkError, ProviderUnavailableError) as err:
            LOGGER.error("Error unliking artist %s: %s", artist_id, err)
            return False

    def _get_throttler(self, kind: str) -> Throttler:
        return self._throttlers.get(kind, self._throttlers["default"])

    def _get_endpoint_lock(self, endpoint: str) -> asyncio.Lock:
        """Return (creating on demand) the per-endpoint serialization lock."""
        lock = self._endpoint_locks.get(endpoint)
        if lock is None:
            lock = asyncio.Lock()
            self._endpoint_locks[endpoint] = lock
        return lock

    @staticmethod
    def _derive_endpoint(func: Callable[..., Any]) -> str | None:
        """
        Extract a stable endpoint key from a lambda's enclosing method.

        Most ``_call_with_retry`` callers pass a lambda defined inside a
        ``YandexMusicClient.<method>``; the lambda's ``__qualname__`` reads as
        ``YandexMusicClient.<method>.<locals>.<lambda>``. We trim the
        ``<locals>...`` suffix to get a per-method endpoint key, which
        mirrors Yandex's per-URL-family edge limit. Returns ``None`` when
        the qualname is missing or not in lambda form — in that case the
        per-endpoint lock is skipped (no behaviour change for that call).
        """
        qn = getattr(func, "__qualname__", "")
        if not qn:
            return None
        if ".<locals>." in qn:
            return qn.split(".<locals>.", 1)[0]
        return qn

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
        """
        Return True if the exception indicates a connection or server drop.

        ``BadRequestError`` upstream extends ``NetworkError`` but represents a
        terminal 4xx response (malformed query, geo-block) — retry-on-reconnect
        would just reproduce the same failure and waste a connection cycle,
        so it is explicitly excluded.
        """
        if isinstance(err, BadRequestError):
            return False
        if isinstance(err, NetworkError) and not self._is_rate_limit_error(err):
            return True
        msg = str(err).lower()
        return "disconnect" in msg or "connection" in msg or "timeout" in msg

    def _classify_429(self, err: Exception) -> Literal["captcha", "rate_limit", "other"]:
        """
        Classify a 429-ish error: smart-captcha edge block vs plain rate-limit.

        Yandex returns an HTML smart-captcha page when its anti-bot edge layer
        decides an endpoint family is too hot. That page is per-endpoint, not
        per-IP, and warrants a longer cooldown than an ordinary 429.
        """
        if not isinstance(err, NetworkError):
            return "other"
        low = str(err).lower()
        is_429 = "429" in low or "too many requests" in low or "rate limit" in low
        if not is_429:
            return "other"
        # 429 payload dump for forensics: which markers actually matched and
        # the first 2000 chars of the body. Captured at DEBUG so a single
        # captcha trip in production can be reconstructed by flipping the
        # provider log level — without flooding steady-state logs.
        if LOGGER.isEnabledFor(logging.DEBUG):
            matched_markers = [m for m in _CAPTCHA_MARKERS if m in low]
            LOGGER.debug(
                "429 classify forensics: markers_matched=%s body[:2000]=%r",
                matched_markers,
                str(err)[:2000],
            )
        return "captcha" if any(m in low for m in _CAPTCHA_MARKERS) else "rate_limit"

    def _is_rate_limit_error(self, err: Exception) -> bool:
        """Return True if the exception indicates a rate-limit response from Yandex."""
        return self._classify_429(err) != "other"

    @staticmethod
    def _truncate_err_msg(err: Exception, limit: int = 200) -> str:
        """Cap a NetworkError message so the captcha HTML body never lands in logs."""
        msg = str(err)
        return msg if len(msg) <= limit else msg[:limit] + "...[truncated]"

    async def _reconnect(self) -> None:
        """
        Disconnect and connect again to recover from Server disconnected / connection errors.

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

    def _check_block(self, kind: str) -> None:
        """
        Raise immediately if `kind` is under a captcha quarantine.

        BYPASS_THROTTLER callers (stream URL refresh) must skip this check so a
        currently playing track isn't dropped mid-stream when an unrelated
        endpoint family trips smart-captcha.
        """
        deadline = self._block_until.get(kind, 0.0)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            raise ResourceTemporarilyUnavailable(
                f"Yandex Music {kind} cooldown active",
                backoff_time=int(remaining) + 1,
            )

    def _trigger_captcha_block(self, kind: str) -> int:
        """
        Quarantine the given throttler kind using the captcha-cooldown ladder.

        Only called when _classify_429 == "captcha". Plain rate-limit responses
        do NOT trigger this, since Yandex's smart-captcha bucket is per
        endpoint family and we don't want to gate unrelated traffic.

        :param kind: Throttler bucket name (e.g. "default", "metadata").
        :return: The cooldown duration in seconds (rounded down to int).
        """
        now = time.monotonic()
        strikes = self._captcha_strikes[kind]
        cutoff = now - CAPTCHA_STRIKE_RETENTION_S
        while strikes and strikes[0] < cutoff:
            strikes.popleft()
        strikes.append(now)
        ladder = CAPTCHA_COOLDOWN_LADDER_S
        idx = min(len(strikes), len(ladder)) - 1
        cooldown = ladder[idx]
        self._block_until[kind] = max(self._block_until.get(kind, 0.0), now + cooldown)
        LOGGER.warning(
            "Yandex Music %s captcha cooldown engaged: %.0fs (strike %d/%d in last %.0fs)",
            kind,
            cooldown,
            len(strikes),
            len(ladder),
            CAPTCHA_STRIKE_RETENTION_S,
        )
        return int(cooldown)

    def _maybe_handle_429(self, err: Exception, kind: str) -> ResourceTemporarilyUnavailable | None:
        """
        Classify a 429 error and build the user-facing exception.

        Returns the prepared `ResourceTemporarilyUnavailable` to raise, or
        ``None`` if the error isn't a 429 (caller should re-raise or fall
        through to connection-error handling). Always truncates the message
        so the smart-captcha HTML body never lands in logs.

        Side effect: a captcha-classified result engages the per-kind block
        deadline. Plain 429 leaves block deadlines untouched.
        """
        classified = self._classify_429(err)
        if classified == "captcha":
            backoff = self._trigger_captcha_block(kind)
            return ResourceTemporarilyUnavailable(
                f"Yandex Music captcha ({kind})",
                backoff_time=backoff,
            )
        if classified == "rate_limit":
            LOGGER.debug("Yandex Music plain 429 on kind=%s", kind)
            return RateLimited(
                "Yandex Music rate limit",
                backoff_time=int(RATE_LIMIT_COOLDOWN_S),
            )
        return None

    def _file_info_cache_get(self, key: tuple[str, str, str, str]) -> dict[str, Any] | None:
        entry = self._file_info_cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._file_info_cache.pop(key, None)
            return None
        self._file_info_cache.move_to_end(key)
        return value

    def _file_info_cache_put(self, key: tuple[str, str, str, str], value: dict[str, Any]) -> None:
        self._file_info_cache[key] = (
            time.monotonic() + FILE_INFO_CACHE_TTL_S,
            value,
        )
        self._file_info_cache.move_to_end(key)
        while len(self._file_info_cache) > FILE_INFO_CACHE_MAX:
            self._file_info_cache.popitem(last=False)

    def _file_info_cache_invalidate(self, track_id: str) -> None:
        for k in [k for k in self._file_info_cache if k[0] == track_id]:
            self._file_info_cache.pop(k, None)

    async def _initial_sync_jitter(self, kind: str) -> None:
        """
        Sleep a small random delay during the first-sync window.

        Smooths out the parallel metadata-refresh burst MA fires immediately
        after a fresh install + auth, which is what triggers smart-captcha
        in #146. After INITIAL_SYNC_WINDOW_S the helper is a no-op — no
        steady-state overhead.

        Only active for the `default` and `metadata` kinds. `file_info` is
        on the streaming hot path (latency matters), and `rotor` has its
        own bucket already tuned for its cadence.

        :param kind: Throttler bucket name.
        """
        if kind not in ("default", "metadata"):
            return
        connected_at = self._connected_at
        if connected_at is None:
            return
        if time.monotonic() - connected_at >= INITIAL_SYNC_WINDOW_S:
            return
        delay = random.uniform(0.0, INITIAL_SYNC_JITTER_S)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _call_with_retry(
        self,
        func: Callable[[ClientAsync], Awaitable[_T]],
        *,
        kind: str = "default",
    ) -> _T:
        """
        Execute an async API call with throttling and one reconnect attempt on connection error.

        Three layers of rate-control apply, outermost first:

        * **Global concurrency cap** (restrictive mode only) — a
          token-wide ``asyncio.Semaphore`` sized to ``RESTRICTIVE_GLOBAL_
          CONCURRENCY``. Keeps total in-flight requests under Yandex's
          per-token edge limit observed on datacenter / VPN IPs (~6).
        * **Per-kind throttler** — a token bucket shared by all calls of a
          given logical class (``default``, ``metadata``, ``file_info``,
          ``rotor``). Caps sustained RPS per kind.
        * **Per-endpoint lock** — derived from ``func.__qualname__`` so each
          ``YandexMusicClient`` method gets its own ``asyncio.Lock``. Caps
          concurrency to 1 per endpoint family — cheap defense-in-depth
          against future ``asyncio.gather`` regressions; near-zero cost
          when there's no contention.

        :param func: Async callable that takes a ClientAsync and returns a result.
        :param kind: Throttler bucket — one of the keys registered in
            ``self._throttlers`` ("default", "metadata", "file_info",
            "rotor"). Falls back to "default" if unknown.
        :return: The result of the API call.
        """
        if self._global_concurrency is not None and not BYPASS_THROTTLER.get():
            async with self._global_concurrency:
                return await self._call_with_retry_inner(func, kind=kind)
        return await self._call_with_retry_inner(func, kind=kind)

    async def _call_with_retry_inner(
        self,
        func: Callable[[ClientAsync], Awaitable[_T]],
        *,
        kind: str,
    ) -> _T:
        """Per-kind throttler + per-endpoint lock layer of ``_call_with_retry``."""
        # Per-request diagnostic — emits caller + kind so a DEBUG-level capture
        # can reconstruct request density before any captcha trip. Stays at
        # DEBUG so steady-state logs are clean.
        if LOGGER.isEnabledFor(logging.DEBUG):
            caller = getattr(func, "__qualname__", "?")
            if ".<locals>." in caller:
                caller = caller.split(".<locals>.")[0]
            LOGGER.debug(
                "req: kind=%s caller=%s bypass=%s",
                kind,
                caller,
                BYPASS_THROTTLER.get(),
            )
        if not BYPASS_THROTTLER.get():
            # Fast path: short-circuit before queueing if the kind is already
            # blocked. Re-check after acquire() — another concurrent request
            # may have engaged the cooldown while we were queued.
            self._check_block(kind)
            await self._initial_sync_jitter(kind)
            await self._get_throttler(kind).acquire()
            self._check_block(kind)
        client = await self._ensure_connected()
        endpoint = self._derive_endpoint(func)
        try:
            return await self._invoke_under_endpoint_lock(func, client, endpoint)
        except Exception as err:
            rate_limit_exc = self._maybe_handle_429(err, kind)
            if rate_limit_exc is not None:
                raise rate_limit_exc from NetworkError(self._truncate_err_msg(err))
            if not self._is_connection_error(err):
                raise
            LOGGER.warning("Connection error, reconnecting and retrying: %s", err)
            try:
                await self._reconnect()
            except Exception as recon_err:
                raise ProviderUnavailableError("Reconnect failed") from recon_err
            client = cast("ClientAsync", self._client)
            # Re-check the block AND re-acquire a throttler token before the
            # retry. Skipping ``acquire()`` here lets reconnect-retries
            # bypass rate-limiting, doubling the effective request rate
            # during connection flap — the conditions that already increase
            # captcha-trip risk. BYPASS_THROTTLER paths skip this so an
            # in-flight stream refresh can still attempt the retry.
            if not BYPASS_THROTTLER.get():
                self._check_block(kind)
                await self._get_throttler(kind).acquire()
                self._check_block(kind)
            # Reconnect-retry must also go through 429 classification —
            # otherwise a captcha on the retry attempt bypasses the cooldown
            # logic and propagates the raw HTML body.
            try:
                return await self._invoke_under_endpoint_lock(func, client, endpoint)
            except Exception as retry_err:
                retry_exc = self._maybe_handle_429(retry_err, kind)
                if retry_exc is not None:
                    raise retry_exc from NetworkError(self._truncate_err_msg(retry_err))
                raise

    async def _invoke_under_endpoint_lock(
        self,
        func: Callable[[ClientAsync], Awaitable[_T]],
        client: ClientAsync,
        endpoint: str | None,
    ) -> _T:
        """Run ``func(client)`` serialised by the per-endpoint lock when set."""
        if endpoint is None:
            return await func(client)
        async with self._get_endpoint_lock(endpoint):
            return await func(client)

    async def _call_no_retry(
        self,
        func: Callable[[ClientAsync], Awaitable[_T]],
        *,
        kind: str = "default",
    ) -> _T:
        """
        Execute an async API call without reconnect retry on call failure.

        Used for fire-and-forget calls (e.g. rotor feedback) where a failed request
        should be silently dropped rather than triggering a reconnect cycle that
        could cause rate limiting. Note: _ensure_connected() is still called to
        establish the initial connection if needed; only the reconnect-on-error
        path is skipped.

        :param func: Async callable that takes a ClientAsync and returns a result.
        :param kind: Throttler bucket — one of the keys registered in
            ``self._throttlers`` ("default", "metadata", "file_info",
            "rotor"). Falls back to "default" if unknown.
        :return: The result of the API call.
        """
        if not BYPASS_THROTTLER.get():
            # Same dual check as _call_with_retry — see comment there.
            self._check_block(kind)
            await self._initial_sync_jitter(kind)
            await self._get_throttler(kind).acquire()
            self._check_block(kind)
        client = await self._ensure_connected()
        try:
            return await func(client)
        except Exception as err:
            # Even on the fire-and-forget path we want to classify 429s: a
            # captcha hit on rotor feedback must still quarantine the rotor
            # kind so the rest of the provider stops poking Yandex's edge
            # while it's hot. Truncation also prevents callers' broad
            # `except NetworkError` from logging multi-KB HTML payloads.
            rate_limit_exc = self._maybe_handle_429(err, kind)
            if rate_limit_exc is not None:
                raise rate_limit_exc from NetworkError(self._truncate_err_msg(err))
            raise

    # Rotor session API (new session-based endpoints)
    #
    # Yandex's newer rotor API models a wave as a long-lived session:
    #   POST /rotor/session/new                     → {radioSessionId, sequence, batchId}
    #   POST /rotor/session/{sessionId}/tracks      → {sequence, batchId}
    #   POST /rotor/session/{sessionId}/feedback    → {result: "ok"}
    # All feedback events carry the same sessionId, so we no longer need to
    # thread per-batch batch_ids through call sites the way the stations-based
    # API forced us to.

    async def _rotor_session_request(
        self, path: str, body: dict[str, Any], *, with_retry: bool = True
    ) -> dict[str, Any] | None:
        """
        POST a JSON body to /rotor/session/{path} and return parsed result.

        Reuses the MarshalX ClientAsync internal request object so we inherit
        its auth headers and parsing. `json=` is forwarded to `aiohttp.request`
        by MarshalX's `**kwargs` passthrough.

        :param path: Path suffix after /rotor/session/ (e.g. "new",
            "{session_id}/tracks", "{session_id}/feedback").
        :param body: JSON body to send.
        :param with_retry: When True (default), uses the same reconnect-on-
            transient-connection-error path as normal data fetches —
            appropriate for ``new`` and ``tracks`` which sit on the
            user-facing browse/play path. Set to False for ``feedback``,
            where a dropped request should be silently lost rather than
            hammered against a potentially rate-limiting server.
        :return: Parsed result dict, or None on failure.
        """

        async def _do(c: ClientAsync) -> dict[str, Any] | None:
            base = getattr(c, "base_url", "https://api.music.yandex.net")
            url = f"{base}/rotor/session/{path}"
            LOGGER.debug("Rotor session POST %s body_keys=%s", path, list(body.keys()))
            try:
                result = await c._request.post(url, json=body)
            except NetworkError as err:
                # Let the outer retry wrapper see transient drops. On the
                # no-retry path swallow ordinary network blips silently, but
                # 429/captcha errors MUST propagate so _call_no_retry can
                # engage the rotor cooldown — otherwise feedback keeps
                # hammering Yandex during an active edge ban.
                if with_retry or self._is_rate_limit_error(err):
                    raise
                LOGGER.debug("Rotor session POST %s: network error (no retry)", path)
                return None
            except BadRequestError as err:
                # 4xx is terminal — server rejected the body; retry would only
                # reproduce the same failure.
                LOGGER.warning("Rotor session POST %s failed: %s", path, err)
                return None
            if isinstance(result, dict):
                LOGGER.debug("Rotor session POST %s → result keys=%s", path, list(result.keys()))
                return result
            LOGGER.debug("Rotor session POST %s → non-dict result: %r", path, result)
            return None

        runner = self._call_with_retry if with_retry else self._call_no_retry
        try:
            return await runner(_do, kind="rotor")
        except UnauthorizedError as err:
            # Expired/invalidated token. Surface as LoginFailed so MA prompts
            # for re-auth instead of the raw yandex_music exception bubbling
            # through browse / play and crashing the caller.
            LOGGER.warning("Rotor session POST %s: token no longer valid", path)
            raise LoginFailed("Invalid Yandex Music token") from err
        except ResourceTemporarilyUnavailable as err:
            LOGGER.warning("Rotor session POST %s rate-limited: %s", path, err)
            return None
        except (NetworkError, ProviderUnavailableError) as err:
            LOGGER.warning("Rotor session POST %s failed: %s", path, self._truncate_err_msg(err))
            return None

    async def _hydrate_session_tracks(self, sequence: list[dict[str, Any]]) -> list[YandexTrack]:
        """
        Extract track IDs from a rotor session sequence and hydrate via get_tracks.

        The session endpoints return tracks inline when includeTracksInResponse
        is true, but full track objects (with download info, covers, etc.) are
        fetched separately so parsed Track objects have the same shape as in
        the rest of the provider.

        :param sequence: List of sequence items from a rotor session response.
        :return: List of full track objects in the same order as `sequence`.
        """
        track_ids: list[str] = []
        for seq in sequence:
            tr = seq.get("track") if isinstance(seq, dict) else None
            tid = None
            if isinstance(tr, dict):
                tid = tr.get("id") or tr.get("track_id")
            if tid is not None:
                track_ids.append(str(tid))
        if not track_ids:
            return []
        try:
            full_tracks = await self.get_tracks(track_ids)
        except ResourceTemporarilyUnavailable as err:
            LOGGER.warning("Rotor session track hydration failed: %s", err)
            return []
        order_map = {str(t.id): t for t in full_tracks if hasattr(t, "id") and t.id}
        return [order_map[tid] for tid in track_ids if tid in order_map]

    async def _get_landing_waves(self, block: str) -> list[dict[str, Any]] | None:
        """
        Fetch wave categories from a /landing-blocks/<block> endpoint.

        Note: Response keys are auto-converted from camelCase to snake_case
        by the yandex-music library's JSON parser.

        :param block: Block name, e.g. 'waves' or 'mixes-waves'.
        :return: List of wave category dicts, or None on error.
        """

        async def _get(c: ClientAsync) -> dict[str, Any]:
            # ``base_url`` is not part of the public ``ClientAsync`` contract;
            # mirror ``_rotor_session_request`` and fall back defensively so a
            # library rename does not crash this endpoint with AttributeError.
            base = getattr(c, "base_url", "https://api.music.yandex.net")
            url = f"{base}/landing-blocks/{block}"
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
