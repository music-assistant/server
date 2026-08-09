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
    Playlist,
    ProviderMapping,
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
from music_assistant.helpers.compare import compare_strings
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
    FragmentAction,
    PandoraFragment,
    PandoraStationSession,
    next_fragment_action,
)
from .helpers import create_auth_headers, get_csrf_token, handle_pandora_error

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence


class StreamViolationError(InvalidDataError):
    """Error raised when Pandora detects concurrent streaming on multiple devices."""


class PandoraProvider(MusicProvider):
    """Pandora Music Provider."""

    _auth_token: str | None = None
    _user_id: str | None = None
    _csrf_token: str | None = None
    _sessions: dict[str, PandoraStationSession]
    _socks_proxy: bool = False
    _high_quality_available: bool = False

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
        if MediaType.PLAYLIST not in media_types:
            return SearchResults()
        results: list[Playlist] = []
        async for station in self._get_stations():
            if compare_strings(station.name, search_query):
                results.append(station)
                if len(results) >= limit:
                    break
        return SearchResults(playlists=results)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve the user's stations as dynamic playlists."""
        async for station in self._get_stations():
            yield station

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full station details by id."""
        async for station in self._get_stations():
            if station.item_id == prov_playlist_id:
                return station
        raise MediaNotFoundError(f"Station {prov_playlist_id} not found")

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """
        Get the currently playable tracks for the given station.

        :param prov_playlist_id: The Pandora station id.
        :param page: Paging index; a station serves a single batch, so anything beyond the
            first page terminates the caller's paging loop.
        """
        if page > 0:
            return []
        session = self._get_or_create_session(prov_playlist_id)
        fragment = session.current
        action = next_fragment_action(fragment, time.time())
        if action is FragmentAction.WITHHOLD:
            # the live fragment's URLs are still pending playback: fetching now would
            # invalidate them, so let the next refill ask again
            return []
        if action is FragmentAction.FETCH or fragment is None:
            fragment = await self._fetch_fragment(session)
        return [self._parse_track(track) for track in fragment.tracks]

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if (track := self._find_track(prov_track_id)) is None:
            raise MediaNotFoundError(f"Track {prov_track_id} not found")
        return self._parse_track(track)

    async def get_album(self, prov_track_id: str) -> Album:
        """Get the album a station track belongs to."""
        if (track := self._find_track(prov_track_id)) and (
            album := self._parse_album(track, prov_track_id)
        ):
            return album
        raise MediaNotFoundError(f"Album {prov_track_id} not found")

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get artist details; Pandora identifies station artists by name only."""
        return self._parse_artist(prov_artist_id)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a station track."""
        if media_type != MediaType.TRACK:
            raise MediaNotFoundError(f"Unsupported media type: {media_type}")
        if "_" not in item_id:
            raise MediaNotFoundError(f"Not a Pandora station track: {item_id}")
        station_id, music_id = item_id.split("_", 1)
        session = self._get_or_create_session(station_id)
        fragment = session.current
        if fragment is None or (track := fragment.find(music_id)) is None:
            # only the newest fragment holds live audio URLs; anything else is unplayable
            raise MediaNotFoundError(f"Track {item_id} is no longer available from Pandora")
        fragment.mark_resolved(music_id, time.time())
        duration = int(track.get("trackLength", 0))
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

                self._user_id = response_data.get("listenerId")

                # Check whether the account is eligible for high-quality streaming.
                try:
                    flags: list[str] = response_data.get("config", {}).get("flags", [])
                    self._high_quality_available = ACCOUNT_FLAG_HIGH_QUALITY in flags
                except AttributeError, TypeError:
                    self._high_quality_available = False

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
            if track.get("audioURL") and "curator message" not in track.get("songTitle", "").lower()
        ]
        return session.add_fragment(tracks, time.time())

    async def _get_stations(self) -> AsyncGenerator[Playlist]:
        """Retrieve the user's stations from the provider."""
        response = await self._api_request("POST", STATIONS_ENDPOINT, data={"pageSize": 250})
        for station in response.get("stations", []):
            yield self._parse_station(station)

    def _get_or_create_session(self, station_id: str) -> PandoraStationSession:
        """Get or create a station session, with LRU eviction if needed."""
        # Simple LRU: limit to 10 active sessions
        if station_id not in self._sessions and len(self._sessions) >= 10:
            oldest = min(self._sessions.values(), key=lambda session: session.last_accessed)
            self.logger.debug("Evicting session for station %s", oldest.station_id)
            del self._sessions[oldest.station_id]
        if station_id not in self._sessions:
            self._sessions[station_id] = PandoraStationSession(station_id)
        session = self._sessions[station_id]
        session.last_accessed = time.time()
        return session

    def _find_track(self, prov_track_id: str) -> dict[str, Any] | None:
        """Return raw track data from a station's retained fragments."""
        if "_" not in prov_track_id:
            return None
        station_id, music_id = prov_track_id.split("_", 1)
        if (session := self._sessions.get(station_id)) is None:
            return None
        return session.find_track(music_id)

    def _parse_station(self, station: dict[str, Any]) -> Playlist:
        """Parse a station object into a dynamic playlist."""
        playlist = Playlist(
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
                (item["url"] for item in art if item.get("size") == 500), art[-1].get("url")
            )
            if art_url:
                playlist.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=art_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                )
        return playlist

    def _parse_track(self, obj: dict[str, Any]) -> Track:
        """Parse a raw fragment track into a Track."""
        name, version = parse_title_and_version(obj.get("songTitle", "Unknown Song"))
        track_id = f"{obj['stationId']}_{obj['musicId']}"
        track = Track(
            item_id=track_id,
            provider=self.domain,
            name=name,
            version=version,
            duration=int(obj.get("trackLength", 0)),
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
                (art["url"] for art in album_art if art.get("size") == 500),
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
        name, version = parse_title_and_version(obj.get("albumTitle", "Unknown Album"))
        return Album(
            item_id=track_id,
            provider=self.domain,
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
            provider=self.domain,
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
