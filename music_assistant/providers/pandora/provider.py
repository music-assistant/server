"""Pandora music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.config_entries import (
    ConfigActionResult,
    ConfigEntry,
    ConfigValueOption,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import (
    CONF_ENTRY_UNOFFICIAL_PROVIDER,
    CONF_PASSWORD,
    CONF_SOCKS_URL,
    CONF_USERNAME,
)
from music_assistant.helpers.aiohttp_client import create_clientsession, get_socks5_url
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ACCOUNT_FLAG_HIGH_QUALITY,
    ADD_FEEDBACK_ENDPOINT,
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    CACHE_CATEGORY_FEEDBACK,
    CONF_QUALITY,
    CONF_TAKEOVER_ACTION,
    DELETE_FEEDBACK_ENDPOINT,
    FEEDBACK_CACHE_EXPIRATION,
    FEEDBACK_NOT_FOUND_ERROR,
    LOGIN_ENDPOINT,
    PLAYBACK_RESUMED_ENDPOINT,
    PLAYLIST_FRAGMENT_ENDPOINT,
    QUALITY_HIGH,
    QUALITY_STANDARD,
    RETRY_REASON_AUTH,
    RETRY_REASON_STREAM_VIOLATION,
    STATIONS_ENDPOINT,
)
from .fragments import (
    MAX_ACTIVE_SESSIONS,
    PandoraFragment,
    PandoraStationSession,
    should_fetch_fragment,
)
from .helpers import (
    create_auth_headers,
    get_csrf_token,
    handle_pandora_error,
    read_account_flags,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence


class StreamViolationError(InvalidDataError):
    """Error raised when Pandora detects concurrent streaming on multiple devices."""


class PandoraProvider(MusicProvider):
    """Pandora Music Provider."""

    _auth_token: str | None = None
    _csrf_token: str | None = None
    _sessions: dict[str, PandoraStationSession]
    _socks_proxy: bool = False
    _high_quality_available: bool = False
    # per instance: Pandora limits the account, and each instance logs in to its own
    throttler: ThrottlerManager
    # held across a whole rating or clear, see set_favorite
    _feedback_lock: asyncio.Lock

    @property
    def max_concurrent_streams(self) -> int:
        """Pandora enforces single-device streaming (stream violation on concurrent use)."""
        return 1

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_QUALITY,
                type=ConfigEntryType.STRING,
                required=True,
                default_value=QUALITY_STANDARD,
                options=[
                    ConfigValueOption(QUALITY_STANDARD),
                    ConfigValueOption(QUALITY_HIGH),
                ],
            ),
            ConfigEntry(
                key=CONF_SOCKS_URL,
                type=ConfigEntryType.STRING,
                required=False,
                default_value="",
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_TAKEOVER_ACTION,
                type=ConfigEntryType.ACTION,
                action=CONF_TAKEOVER_ACTION,
                required=False,
            ),
        )

    async def handle_config_action(
        self, action: str
    ) -> tuple[ConfigEntry, ...] | ConfigActionResult | None:
        """Handle a one-shot config action button press."""
        if action == CONF_TAKEOVER_ACTION:
            await self.takeover_stream()
            return None
        return await super().handle_config_action(action)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._sessions = {}
        # every request takes a slot, the login included
        self.throttler = ThrottlerManager(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD)
        self._feedback_lock = asyncio.Lock()

        # Authenticate with Pandora
        username = str(self.get_setup_value(CONF_USERNAME) or "")
        password = str(self.get_setup_value(CONF_PASSWORD) or "")
        if not username.strip() or not password.strip():
            raise LoginFailed("Username and password are required")
        socks_url = get_socks5_url(str(self.config.get_value(CONF_SOCKS_URL)))

        if socks_url:
            self.http_session = create_clientsession(
                self.mass, verify_ssl=True, socks_url=socks_url
            )
            self._socks_proxy = True
        else:
            self.http_session = self.mass.http_session
        try:
            await self._authenticate(username, password)
        except MusicAssistantError, TimeoutError:
            # a provider that fails to initialize is never registered, so unload() will not
            # run to close the proxy session created above
            await self.close()
            raise

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self.close()
        if is_removed:
            # the cache cleanup for a removed provider skips persistent entries, and the
            # feedback ids are kept persistently, so they would outlive the instance
            await self.mass.cache.delete(
                None, category=CACHE_CATEGORY_FEEDBACK, provider=self.instance_id
            )
        await super().unload(is_removed)

    async def close(self) -> None:
        """Handle closing of http session if using socks."""
        if self._socks_proxy and self.http_session:
            await self.http_session.close()

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse the user's Pandora stations."""
        sub_path = path.split("://", 1)[1] if "://" in path else ""
        if sub_path:
            return await super().browse(path)
        return [station async for station in self._get_stations()]

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 25,
    ) -> SearchResults:
        """Search the user's stations by name."""
        # search is limited to the user's own stations: the API's catalogue search
        # requires the legacy endpoints this provider does not speak
        if MediaType.RADIO not in media_types:
            return SearchResults()
        # substring rather than compare_strings: that helper answers "are these the same
        # entity", and its fuzzy mode rejects a length difference over four characters, so a
        # short query like "rock" could never reach a station called "Classic Rock Radio"
        query = search_query.lower().strip()
        if not query:
            # every name contains the empty string, so an empty query would match the whole
            # library rather than nothing
            return SearchResults()
        results: list[Radio] = []
        async for station in self._get_stations():
            if query in station.name.lower():
                results.append(station)
                if len(results) >= limit:
                    break
        return SearchResults(radio=results)

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Retrieve the user's stations as dynamic radio stations."""
        async for station in self._get_stations():
            yield station

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full station details by id."""
        async for station in self._get_stations():
            if station.item_id == prov_radio_id:
                return station
        raise MediaNotFoundError(f"Station {prov_radio_id} not found")

    async def get_dynamic_radio_tracks(self, prov_radio_id: str) -> list[Track]:
        """
        Get the currently playable tracks for the given station.

        :param prov_radio_id: The Pandora station id.
        """
        session = self._get_or_create_session(prov_radio_id)
        fragment = session.current
        if fragment is None or should_fetch_fragment(fragment, time.time()):
            fragment = await self._fetch_fragment(session)
        # always serve the live fragment: an empty list would read as "this station has
        # ended" to the queue controller, which stops playback instead of continuing it.
        # Already-served tracks are withheld: the queue controller only de-duplicates refill
        # candidates against its unplayed tail, so a served track that scrolls out of that
        # tail would otherwise be re-added here and then fail once the fragment has moved on.
        return [self._parse_track(track) for track in fragment.pending]

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if (track := self._find_track(prov_track_id)) is None:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return self._parse_track(track)

    async def get_album(self, prov_album_id: str) -> Album:
        """
        Get the album a station track belongs to.

        Fragments carry no album identifier of their own, so an album is addressed by the id of
        one of its tracks - see `_parse_album`, which mints the album that way.
        """
        if (track := self._find_track(prov_album_id)) and (
            album := self._parse_album(track, prov_album_id)
        ):
            return album
        raise MediaNotFoundError(f"Album {prov_album_id} not found")

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Get the tracks of an album, listed but not playable.

        :param prov_album_id: The Pandora album id.
        """
        if (track := self._find_track(prov_album_id)) is None:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        # Pandora has no album catalogue, so the album only holds the station track it was
        # created from. Playing a chosen station track on demand is a paid Pandora feature.
        return [self._parse_track(track, available=False)]

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details; Pandora identifies station artists by name only."""
        return self._parse_artist(prov_artist_id)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a station track."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Unsupported media type: {media_type}")
        now = time.time()
        # only each session's live fragment: an older one's signed URL may already be expired
        # and there is no way to tell from here, so refuse rather than hand ffmpeg a link
        # that 403s mid-track
        holders = [
            (fragment, track)
            for session in self._sessions.values()
            if (fragment := session.current) is not None
            and (track := fragment.find(item_id)) is not None
        ]
        playable = [holder for holder in holders if not holder[0].urls_expired(now)]
        if not playable:
            if holders:
                # the signed URLs have outlived their TTL, which is what a long pause looks
                # like from here. Refusing keeps the failure named rather than an opaque
                # ffmpeg error. Note this asks a different question from is_stale: a fragment
                # can be idle long enough to be worth replacing while its URLs are still
                # perfectly playable, and refusing those would break resuming after a pause.
                raise MediaNotFoundError(f"Track {item_id} expired while playback was stopped")
            raise MediaNotFoundError(f"Track {item_id} is no longer available from Pandora")
        # stations overlap, so the same song can sit in several sessions at once. Serve the
        # freshest copy, not the one from whichever session happens to be oldest: an older
        # station's expired fragment must not fail a playable track, and the fragment that is
        # marked as having served the track has to be the one the audio URL came from.
        fragment, track = max(playable, key=lambda holder: holder[0].fetched_at)
        fragment.mark_resolved(item_id, now)
        duration = int(track.get("trackLength") or 0)
        can_seek = duration > 0
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=self._audio_format(),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=track["audioURL"],
            duration=duration,
            can_seek=can_seek,
            allow_seek=can_seek,
        )

    async def set_favorite(
        self, prov_item_id: str, media_type: MediaType, favorite: bool | None
    ) -> None:
        """
        Send a like or dislike on a station track to Pandora as thumbs up or thumbs down.

        :param prov_item_id: The track's Pandora id (`TR:…`).
        :param media_type: Media type of the item, only tracks can be rated.
        :param favorite: True for thumbs up, False for thumbs down, None to remove the thumb.
        """
        if media_type != MediaType.TRACK:
            return
        # Ratings change one at a time: each reads the kept feedback, waits on Pandora and writes
        # it back, so an overlapping one (MA runs dislikes and clears as background tasks) would
        # overwrite what the other kept. The track is looked up before waiting, so a refusal
        # never waits and the token is the one served when the user rated.
        if favorite is None:
            async with self._feedback_lock:
                await self._clear_feedback(prov_item_id)
            return
        # Pandora ties feedback to the trackToken it issued with a fragment, and that token
        # decides which station the feedback trains, so only a track still held in a retained
        # fragment can be rated. Pandora has no other way to add a thumb. MA has saved the
        # rating before calling here, so a rating that cannot be sent is logged, not raised:
        # an error would undo nothing and only confuse.
        track = self._find_rated_track(prov_item_id)
        if track is None:
            self._log_unsent_rating(
                prov_item_id,
                "it is not in any retained Pandora fragment (not playing, or played too long ago)",
            )
            return
        if track.get("allowFeedback") is False:
            self._log_unsent_rating(prov_item_id, "Pandora does not allow feedback on it")
            return
        if not (token := track.get("trackToken")):
            self._log_unsent_rating(prov_item_id, "Pandora served it without a trackToken")
            return
        async with self._feedback_lock:
            result = await self._api_request(
                "POST", ADD_FEEDBACK_ENDPOINT, data={"trackToken": token, "isPositive": favorite}
            )
            # Pandora removes feedback only by the id it answers with here, and by the time a
            # rating is cleared the fragment that held the track is long gone, so keep the id
            await self._remember_feedback(prov_item_id, result)

    async def takeover_stream(self) -> None:
        """
        Force Pandora to end any other active session and resume here.

        This sends "forceActive=true" to the playbackResumed endpoint, which instructs Pandora to
        terminate any conflicting stream on other devices. The user must manually restart playback
        in MA after clicking the config button that triggers this call.
        """
        self.logger.debug("Sending playbackResumed request to Pandora to attempt stream takeover.")
        await self._api_request(
            "POST",
            PLAYBACK_RESUMED_ENDPOINT,
            data={"forceActive": True},
            # This is called as part of handling a STREAM_VIOLATION 429, so mark that reason as
            # already exhausted to prevent _api_request from retrying on another 429.
            exhausted_retry_reasons=frozenset({RETRY_REASON_STREAM_VIOLATION}),
        )

    async def _authenticate(self, username: str, password: str) -> None:
        """Authenticate with Pandora and get auth token."""
        try:
            async with self.throttler.acquire():
                self._csrf_token = await get_csrf_token(self.http_session)

            login_data = {
                "username": username,
                "password": password,
                "keepLoggedIn": True,
                "existingAuthToken": None,
            }

            headers = create_auth_headers(self._csrf_token)

            async with (
                self.throttler.acquire(),
                self.http_session.post(
                    LOGIN_ENDPOINT,
                    headers=headers,
                    json=login_data,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response,
            ):
                if response.status != 200:
                    raise LoginFailed(f"Login request failed with status {response.status}")

                response_data = await response.json()
                handle_pandora_error(response_data)

                self._auth_token = response_data.get("authToken")
                if not self._auth_token:
                    raise LoginFailed("No auth token received from Pandora")

                # What this account is entitled to. Pandora sends config and flags as null
                # on some accounts, so read through them rather than guarding after the fact.
                flags = read_account_flags(response_data)
                self._high_quality_available = ACCOUNT_FLAG_HIGH_QUALITY in flags

                self.logger.info(
                    "Successfully authenticated with Pandora "
                    "(high-quality streaming available: %s)",
                    self._high_quality_available,
                )

        except aiohttp.ClientError as err:
            self.logger.exception("Network error during authentication")
            raise ProviderUnavailableError(
                "Unable to connect to Pandora for authentication"
            ) from err

    async def _api_request(
        self,
        method: str,
        url: str,
        data: dict[str, Any] | None = None,
        exhausted_retry_reasons: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """
        Make an API request to Pandora.

        :param method: HTTP method (GET, POST, etc.)
        :param url: API endpoint URL
        :param data: Optional JSON data to send
        :param exhausted_retry_reasons: Set of retry reasons already attempted for this request.
            Pass a pre-populated set to prevent specific retry strategies from being attempted.
        """
        if not self._csrf_token or not self._auth_token:
            raise LoginFailed("Not authenticated with Pandora")

        headers = create_auth_headers(self._csrf_token, self._auth_token)

        try:
            # throttled here rather than with throttle_with_retries: that decorator retries on
            # ResourceTemporarilyUnavailable, which handle_pandora_error also raises for
            # parameter errors that no retry fixes, and the writes (feedback) must not repeat
            async with (
                self.throttler.acquire(),
                self.http_session.request(method, url, json=data, headers=headers) as response,
            ):
                # Check status BEFORE parsing JSON
                if response.status == 401:
                    if RETRY_REASON_AUTH not in exhausted_retry_reasons:
                        # Auth token expired, re-authenticate and retry once
                        username = str(self.get_setup_value(CONF_USERNAME) or "")
                        password = str(self.get_setup_value(CONF_PASSWORD) or "")
                        await self._authenticate(username, password)
                        return await self._api_request(
                            method,
                            url,
                            data,
                            exhausted_retry_reasons=exhausted_retry_reasons | {RETRY_REASON_AUTH},
                        )
                    raise LoginFailed("Pandora authentication failed after retry")
                if response.status == 404:
                    raise MediaNotFoundError("Resource not found")
                if response.status == 429:
                    # Another device may already be streaming on this account.
                    # Parse the body to confirm it is a STREAM_VIOLATION.
                    try:
                        error_body: dict[str, Any] = await response.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as err:
                        raise InvalidDataError(
                            "Unable to parse error 429 response body from Pandora"
                        ) from err
                    if error_body.get("errorString") == "STREAM_VIOLATION":
                        if RETRY_REASON_STREAM_VIOLATION not in exhausted_retry_reasons:
                            self.logger.warning(
                                "Pandora stream is already active on another device. "
                                "Automatically taking over the stream and retrying the request."
                            )
                            await self.takeover_stream()
                            return await self._api_request(
                                method,
                                url,
                                data,
                                exhausted_retry_reasons=exhausted_retry_reasons
                                | {RETRY_REASON_STREAM_VIOLATION},
                            )
                        raise StreamViolationError("STREAM_VIOLATION")
                    # This is some other, not concurrent streaming error kind of 429
                    raise ProviderUnavailableError(f"Pandora rate-limited (HTTP 429): {error_body}")
                if response.status >= 500:
                    raise ProviderUnavailableError("Pandora server error")
                if response.status >= 400:
                    # a clear must tell a thumb removed outside Music Assistant from a delete
                    # that failed, and only the body says which
                    try:
                        body = await response.json()
                    except aiohttp.ContentTypeError, json.JSONDecodeError:
                        body = None
                    error = body.get("errorString") if isinstance(body, dict) else None
                    if error == FEEDBACK_NOT_FOUND_ERROR:
                        raise MediaNotFoundError(
                            f"Pandora feedback not found: {body.get('message')}"
                        )
                    raise InvalidDataError(f"Pandora API error: HTTP {response.status}")

                result: dict[str, Any] = await response.json()
                handle_pandora_error(result)
                return result

        except aiohttp.ClientError as err:
            raise ProviderUnavailableError("Unable to connect to Pandora") from err
        except (ValueError, KeyError) as err:
            raise InvalidDataError("Invalid response from Pandora") from err

    async def _fetch_fragment(self, session: PandoraStationSession) -> PandoraFragment:
        """Fetch the next fragment for a station and retain it as the live one."""
        is_station_start = not session.fragments
        try:
            result: dict[str, Any] = await self._api_request(
                "POST",
                PLAYLIST_FRAGMENT_ENDPOINT,
                data={
                    "stationId": session.station_id,
                    "isStationStart": is_station_start,
                    "fragmentRequestReason": "Normal",
                    "audioFormat": "mp3-hifi" if self._use_high_quality() else "aacplus",
                    "startingAtTrackId": None,
                    "onDemandArtistMessageArtistUidHex": None,
                    "onDemandArtistMessageIdHex": None,
                },
                # Mark stream violation retry as already exhausted for non-initial fragments
                # this prevents us from fighting with the concurrent streaming limit
                # if the user starts a stream on a different device while MA is already playing.
                exhausted_retry_reasons=frozenset()
                if is_station_start
                else frozenset({RETRY_REASON_STREAM_VIOLATION}),
            )
        except StreamViolationError:
            self.logger.warning(
                "Pandora stream is already active on another device. "
                "To manually take over the stream on this device, use the "
                "'Take over stream' button on the provider configuration page.",
            )
            raise
        except InvalidDataError as err:
            self.logger.error("Invalid fragment data for station %s: %s", session.station_id, err)
            raise
        tracks = [
            track
            for track in result.get("tracks", [])
            if track.get("audioURL")
            and track.get("pandoraId")
            and "curator message" not in (track.get("songTitle") or "").lower()
        ]
        if not tracks:
            # retaining an empty fragment would make it the live one, and nothing can ever
            # spend it — the station would serve nothing until the staleness window elapsed
            raise MediaNotFoundError(
                f"Pandora returned no playable tracks for {session.station_id}"
            )
        return session.add_fragment(tracks, time.time())

    async def _get_stations(self) -> AsyncGenerator[Radio]:
        """Retrieve the user's stations from the provider."""
        response = await self._api_request("POST", STATIONS_ENDPOINT, data={"pageSize": 250})
        for station in response.get("stations", []):
            yield self._parse_station(station)

    def _get_or_create_session(self, station_id: str) -> PandoraStationSession:
        """Get or create a station session, with LRU eviction if needed."""
        if station_id not in self._sessions and len(self._sessions) >= MAX_ACTIVE_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda session: session.last_accessed)
            self.logger.debug("Evicting session for station %s", oldest.station_id)
            del self._sessions[oldest.station_id]
        if station_id not in self._sessions:
            self._sessions[station_id] = PandoraStationSession(station_id)
        session = self._sessions[station_id]
        session.last_accessed = time.time()
        return session

    def _find_track(self, prov_track_id: str) -> dict[str, Any] | None:
        """
        Return raw track data from the freshest retained fragment holding it, or None.

        The id no longer names a station, so every retained session is searched. At most
        `MAX_ACTIVE_SESSIONS` sessions hold at most `MAX_RETAINED_FRAGMENTS` fragments of about
        four tracks each, so this stays small.
        Stations overlap, so the freshest fragment decides: it is the most recent answer
        Pandora gave for the track, and picking by dict order instead would let the same
        song resolve differently from one lookup to the next.
        """
        holders = [
            (fragment, track)
            for session in self._sessions.values()
            for fragment in session.fragments
            if (track := fragment.find(prov_track_id)) is not None
        ]
        freshest = max(holders, key=lambda holder: holder[0].fetched_at, default=None)
        return freshest[1] if freshest is not None else None

    def _find_rated_track(self, prov_track_id: str) -> dict[str, Any] | None:
        """Return raw track data from the retained fragment that last served it, or None."""
        # the copy that most recently went to the audio pipeline names the station that played
        # the song; the freshest fetch would train the wrong station whenever another station
        # fetches the same song after playback started. With several players on different
        # stations playing the same song, the latest stream wins: set_favorite gets no player.
        served = [
            (fragment.served[prov_track_id], track)
            for session in self._sessions.values()
            for fragment in session.fragments
            if prov_track_id in fragment.served
            and (track := fragment.find(prov_track_id)) is not None
        ]
        if served:
            return max(served, key=lambda holder: holder[0])[1]
        # never streamed, e.g. a track rated while still queued
        return self._find_track(prov_track_id)

    def _log_unsent_rating(self, prov_track_id: str, reason: str) -> None:
        """Log that a rating stays in Music Assistant without reaching Pandora, and why."""
        self.logger.warning(
            "The rating on track %s is kept in Music Assistant but not sent to Pandora: %s",
            prov_track_id,
            reason,
        )

    async def _remember_feedback(self, prov_track_id: str, result: dict[str, Any]) -> None:
        """
        Keep the feedback Pandora recorded for a track, so a later clear can delete it.

        :param prov_track_id: The track's Pandora id.
        :param result: Pandora's addFeedback response.
        """
        if not (feedback_id := result.get("feedbackId")):
            self.logger.warning(
                "Pandora recorded feedback on track %s without a feedback id; clearing this "
                "rating will not remove it from Pandora",
                prov_track_id,
            )
            return
        station_id = result.get("stationId")
        # Pandora holds one thumb per track and station: a new thumb on the same station
        # replaces the kept one, a thumb on another station is kept beside it; without a
        # station the new thumb cannot be matched, so every kept one stays
        kept = [
            entry
            for entry in await self._get_kept_feedback(prov_track_id)
            if station_id is None or entry.get("stationId") != station_id
        ]
        kept.append(
            {
                "feedbackId": feedback_id,
                "stationId": station_id,
                "isPositive": bool(result.get("isPositive")),
            }
        )
        await self._set_kept_feedback(prov_track_id, kept)

    async def _clear_feedback(self, prov_track_id: str) -> None:
        """
        Delete the feedback this provider sent to Pandora for a track, on every station.

        :param prov_track_id: The track's Pandora id.
        """
        kept = await self._get_kept_feedback(prov_track_id)
        if not kept:
            # rated before feedback ids were kept, or outside Music Assistant
            self.logger.warning(
                "No Pandora feedback is known for track %s, so clearing its rating cannot "
                "remove a thumb on Pandora; remove it in the Pandora app",
                prov_track_id,
            )
            return
        # every entry is tried, so one that fails does not leave the others on Pandora
        remaining: list[dict[str, Any]] = []
        failure: Exception | None = None
        for entry in kept:
            try:
                # Pandora deletes by id whatever isPositive says, so a thumb flipped in the
                # Pandora app since is deleted too
                await self._api_request(
                    "POST",
                    DELETE_FEEDBACK_ENDPOINT,
                    data={"feedbackId": entry["feedbackId"], "isPositive": entry["isPositive"]},
                )
            except MediaNotFoundError:
                # removed outside Music Assistant (in the Pandora app, or with its station), so
                # nothing is left to delete; keeping it would fail every clear from now on
                self.logger.debug(
                    "Pandora no longer has feedback %s on track %s",
                    entry["feedbackId"],
                    prov_track_id,
                )
            except (MusicAssistantError, TimeoutError) as err:
                # keep what may still be on Pandora, so another clear can retry it
                remaining.append(entry)
                failure = failure or err
        if failure is not None:
            await self._set_kept_feedback(prov_track_id, remaining)
            raise failure
        await self.mass.cache.delete(
            prov_track_id, category=CACHE_CATEGORY_FEEDBACK, provider=self.instance_id
        )

    async def _get_kept_feedback(self, prov_track_id: str) -> list[dict[str, Any]]:
        """Return the feedback kept for a track, one entry per station."""
        kept = await self.mass.cache.get(
            prov_track_id, provider=self.instance_id, category=CACHE_CATEGORY_FEEDBACK
        )
        return list(kept) if isinstance(kept, list) else []

    async def _set_kept_feedback(self, prov_track_id: str, kept: list[dict[str, Any]]) -> None:
        """Store the feedback kept for a track, surviving restarts and cache clears."""
        await self.mass.cache.set(
            prov_track_id,
            kept,
            expiration=FEEDBACK_CACHE_EXPIRATION,
            provider=self.instance_id,
            category=CACHE_CATEGORY_FEEDBACK,
            persistent=True,
        )

    def _parse_station(self, station: dict[str, Any]) -> Radio:
        """Parse a station object into a dynamic radio station."""
        radio = Radio(
            item_id=station["stationId"],
            provider=self.instance_id,
            name=station["name"],
            is_dynamic=True,
            provider_mappings={
                ProviderMapping(
                    item_id=station["stationId"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if art := station.get("art"):
            art_url = next(
                (item.get("url") for item in art if item.get("size") == 500), art[-1].get("url")
            )
            if art_url:
                radio.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=art_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                )
        return radio

    def _parse_track(self, obj: dict[str, Any], available: bool = True) -> Track:
        """
        Parse a raw fragment track into a Track.

        :param obj: The raw fragment track.
        :param available: Whether the track can be played from this listing.
        """
        name, version = parse_title_and_version(obj.get("songTitle") or "Unknown Song")
        track_id = obj["pandoraId"]
        track = Track(
            item_id=track_id,
            provider=self.instance_id,
            name=name,
            version=version,
            duration=int(obj.get("trackLength") or 0),
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    audio_format=self._audio_format(),
                    url=obj.get("songDetailURL"),
                    available=available,
                )
            },
        )
        if album_art := obj.get("albumArt"):
            art_url = next(
                (art.get("url") for art in album_art if art.get("size") == 500),
                album_art[-1].get("url"),
            )
            if art_url:
                track.metadata.add_image(
                    MediaItemImage(
                        provider=self.instance_id,
                        type=ImageType.THUMB,
                        path=art_url,
                        remotely_accessible=True,
                    )
                )
        if artist_name := obj.get("artistName"):
            track.artists = UniqueList([self._parse_artist(artist_name)])
        track.album = self._parse_album(obj, track_id)
        return track

    def _parse_album(self, obj: dict[str, Any], track_id: str) -> Album | None:
        """Parse the album a fragment track belongs to, if the API named one."""
        if not (url := obj.get("albumDetailURL")):
            return None
        name, version = parse_title_and_version(obj.get("albumTitle") or "Unknown Album")
        return Album(
            item_id=track_id,
            provider=self.instance_id,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=url,
                )
            },
        )

    def _parse_artist(self, artist_name: str) -> Artist:
        """Parse an artist; Pandora fragments identify artists by name only."""
        return Artist(
            item_id=artist_name,
            name=artist_name,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=artist_name,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    def _audio_format(self) -> AudioFormat:
        """Return the audio format the fragments are requested in."""
        return AudioFormat(
            content_type=ContentType.MP3 if self._use_high_quality() else ContentType.AAC
        )

    def _use_high_quality(self) -> bool:
        """
        Whether high quality audio should be requested from Pandora.

        This allows a graceful fallback to standard quality if the account is not eligible for
        high-quality streaming, while still respecting the user's preference if they are eligible.
        """
        return self._high_quality_available and self.config.get_value(CONF_QUALITY) == QUALITY_HIGH
