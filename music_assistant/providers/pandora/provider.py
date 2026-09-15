"""Pandora music provider for Music Assistant."""

from __future__ import annotations

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
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ACCOUNT_FLAG_HIGH_QUALITY,
    CONF_QUALITY,
    CONF_TAKEOVER_ACTION,
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
        await self._authenticate(username, password)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self.close()
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

    async def get_dynamic_radio_tracks(
        self, prov_radio_id: str, *, sample: bool = False
    ) -> list[Track]:
        """
        Get the currently playable tracks for the given station.

        :param prov_radio_id: The Pandora station id.
        :param sample: Ignored; the station session already serves every caller the
            same live fragment without consuming it.
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
            self._csrf_token = await get_csrf_token(self.http_session)

            login_data = {
                "username": username,
                "password": password,
                "keepLoggedIn": True,
                "existingAuthToken": None,
            }

            headers = create_auth_headers(self._csrf_token)

            async with self.http_session.post(
                LOGIN_ENDPOINT,
                headers=headers,
                json=login_data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    await self.close()
                    raise LoginFailed(f"Login request failed with status {response.status}")

                response_data = await response.json()
                handle_pandora_error(response_data)

                self._auth_token = response_data.get("authToken")
                if not self._auth_token:
                    await self.close()
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
            await self.close()
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
            await self.close()
            raise LoginFailed("Not authenticated with Pandora")

        headers = create_auth_headers(self._csrf_token, self._auth_token)

        try:
            async with self.http_session.request(
                method, url, json=data, headers=headers
            ) as response:
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
                    await self.close()
                    raise LoginFailed("Pandora authentication failed after retry")
                if response.status == 404:
                    await self.close()
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
                    await self.close()
                    raise ProviderUnavailableError("Pandora server error")
                if response.status >= 400:
                    await self.close()
                    raise InvalidDataError(f"Pandora API error: HTTP {response.status}")

                result: dict[str, Any] = await response.json()
                handle_pandora_error(result)
                return result

        except aiohttp.ClientError as err:
            await self.close()
            raise ProviderUnavailableError("Unable to connect to Pandora") from err
        except (ValueError, KeyError) as err:
            await self.close()
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
        except MediaNotFoundError:
            await self.close()
            raise
        except StreamViolationError:
            self.logger.warning(
                "Pandora stream is already active on another device. "
                "To manually take over the stream on this device, use the "
                "'Take over stream' button on the provider configuration page.",
            )
            raise
        except InvalidDataError as err:
            self.logger.error("Invalid fragment data for station %s: %s", session.station_id, err)
            await self.close()
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

    def _parse_track(self, obj: dict[str, Any]) -> Track:
        """Parse a raw fragment track into a Track."""
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
