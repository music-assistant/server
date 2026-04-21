"""Pandora music provider for Music Assistant."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import aiohttp
from music_assistant_models.enums import (
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
    Radio,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_PASSWORD, CONF_SOCKS_URL, CONF_USERNAME
from music_assistant.helpers.aiohttp_client import create_clientsession, get_socks5_url
from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ACCOUNT_FLAG_HIGH_QUALITY,
    CONF_QUALITY,
    LOGIN_ENDPOINT,
    PLAYBACK_RESUMED_ENDPOINT,
    PLAYLIST_FRAGMENT_ENDPOINT,
    QUALITY_HIGH,
    RETRY_REASON_AUTH,
    RETRY_REASON_STREAM_VIOLATION,
    STATIONS_ENDPOINT,
)
from .helpers import create_auth_headers, get_csrf_token, handle_pandora_error


async def read_file_to_string(file_path: str) -> str:
    """Read file content and return as string."""

    def sync_read() -> str:
        with open(file_path, encoding="utf-8") as file:
            return file.read().strip()

    return await asyncio.to_thread(sync_read)


class PandoraStationSession:
    """Manages streaming state for a single Pandora station."""

    def __init__(self, station_id: str):
        """
        Initialize a new station streaming session.

        Args:
            station_id: The Pandora station ID.
        """
        self.station_id = station_id
        self.last_track_started: bool = False
        self.fragments: list[list[dict[str, Any]]] = []
        self.last_accessed = time.time()


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

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._sessions = {}

        # Authenticate with Pandora
        username = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD))
        socks_url = get_socks5_url(str(self.config.get_value(CONF_SOCKS_URL)))

        if socks_url:
            self.http_session = create_clientsession(
                self.mass, verify_ssl=True, socks_url=socks_url
            )
            self._socks_proxy = True
        else:
            self.http_session = self.mass.http_session
        if token_file := os.environ.get("PANDORA_AUTHTOKEN_FILE"):
            if os.path.exists(token_file):
                self._auth_token = await read_file_to_string(token_file)
        await self._authenticate(username, password)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await self.close()
        await super().unload(is_removed)

    async def _authenticate(self, username: str, password: str) -> None:
        """Authenticate with Pandora and get auth token."""
        try:
            self._csrf_token = await get_csrf_token(self.http_session)
            if self._auth_token:
                self.logger.warning(f"Using existing auth token: {self._auth_token}")
                return

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
                except (AttributeError, TypeError):
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
                        username = str(self.config.get_value(CONF_USERNAME))
                        password = str(self.config.get_value(CONF_PASSWORD))
                        self._auth_token = None
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

    #    @use_cache(3600)
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get single radio station details."""
        return Radio(
            item_id=prov_radio_id,
            provider=self.domain,
            name=f"Pandora Station {prov_radio_id}",
            provider_mappings={
                ProviderMapping(
                    item_id=prov_radio_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse Pandora radio stations."""
        sub_path = path.split("://", 1)[1] if "://" in path else ""
        if not sub_path:
            result: list[MediaItemType | ItemMapping | BrowseFolder] = []
            async for station in self._get_stations():
                self.logger.info(f"Retrieved {station.name}, {station.is_dynamic}")
                result.append(station)
            return result
        return await super().browse(path)

    async def _get_stations(self) -> AsyncGenerator[Playlist]:
        """Retrieve library/subscribed radio stations from the provider."""
        response = await self._api_request(
            "POST",
            STATIONS_ENDPOINT,
            data={
                "pageSize": 250,
            },
        )
        stations = response.get("stations", [])
        self.logger.warning("Retrieved %d stations from Pandora", len(stations))
        for station in stations:
            yield self._parse_station(station)

    def _find_track(self, prov_track_id: str) -> dict[str, Any]:
        """Find track in all station fragments from provider track_id."""
        station_id, track_id = prov_track_id.split("_")
        session = self._get_or_create_session(station_id)
        for tracks in session.fragments[::-1]:
            for track in tracks:
                if track.get("musicId") == track_id:
                    return track
        return {}

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if track := self._find_track(prov_track_id):
            return self._parse_track(track)
        raise MediaNotFoundError(f"Track {prov_track_id} not found")

    async def get_artist(self, artist_name: str) -> Artist:
        """Get artist details from just artist_name."""
        if artist := self._parse_artist(artist_name):
            return artist
        raise MediaNotFoundError(f"Artist {artist_name} not found")

    async def get_album(self, prov_track_id: str) -> Album:
        """Get Album from provider track_id."""
        if track := self._find_track(prov_track_id):
            if album := self._parse_album(track, prov_track_id):
                return album
        raise MediaNotFoundError(f"Album {prov_track_id} not found")

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        self.logger.warning(f"get playlist {prov_playlist_id}")
        async for station in self._get_stations():
            if station.item_id == prov_playlist_id:
                self.logger.warning(f"returning playlist station {station.name}")
                return station
        return self._parse_station({"stationId": prov_playlist_id})

    async def get_playlist_tracks(self, station_id: str, page: int = 0) -> list[Track]:
        """Get all playlist tracks for given station id."""
        if page > 0:
            return []

        session = self._get_or_create_session(station_id)
        self.logger.warning(
            f"getting tracks for {station_id} -- {len(session.fragments)} {session.last_track_started}"
        )
        if len(session.fragments) == 0 or session.last_track_started:
            await self._get_fragment_data(session, len(session.fragments))
            if tracks := session.fragments[-1]:
                return [self._parse_track(track) for track in tracks]
        return []

    def _parse_station(self, station: dict[str, Any]) -> Playlist:
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
                (item["url"] for item in art if item.get("size") == 500),
                art[-1]["url"] if art else None,
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
        playlist.metadata.description = "Pandora Radio Station"
        return playlist

    def _parse_track(self, obj: dict[str, Any]) -> Track:
        """Parse track object to generic layout."""
        name, version = parse_title_and_version(obj.get("songTitle", "Unknown Song"))
        track_id = obj["stationId"] + "_" + obj["musicId"]
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
                    audio_format=AudioFormat(
                        content_type=ContentType.MP3
                        if self._use_high_quality()
                        else ContentType.AAC,
                    ),
                    url=obj.get("songDetailURL"),
                )
            },
        )
        if album_art := obj.get("albumArt"):
            album_art_url = next(
                (art["url"] for art in album_art if art.get("size") == 500),
                album_art[-1]["url"] if album_art else None,
            )
            if album_art_url:
                track.metadata.add_image(
                    MediaItemImage(
                        provider=self.instance_id,
                        type=ImageType.THUMB,
                        path=album_art_url,
                        remotely_accessible=True,
                    )
                )
        if artist_name := obj.get("artistName"):
            track.artists = UniqueList([self._parse_artist(artist_name)])
        track.album = self._parse_album(obj, track_id)
        self.logger.info(name)
        return track

    def _parse_artist(self, artist_name: str) -> Artist:
        """Parse artist object to generic layout."""
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

    def _parse_album(self, obj: dict[str, Any], track_id: str) -> Album | None:
        """Parse track object to generic layout."""
        name, version = parse_title_and_version(obj.get("albumTitle", "Unknown Album"))
        if url := obj.get("albumDetailURL"):
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
        return None

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        await super().loaded_in_mass()
        for player in self.mass.players.all_players(return_disabled=True):
            items = self.mass.player_queues.items(player.player_id)
            self.logger.warning(f"player {player.provider_id}, {player.player_id} {len(items)}")
            if items:
                item = items[-1]
                if item.media_item:
                    self.logger.warning(f"item {item.media_item.provider}, {item.media_item.name}")
                    if self._find_track(item.media_item.item_id) == {}:
                        self.logger.warning(f"please clear this queue {item.queue_id}")
                        self.mass.player_queues.clear(item.queue_id)

    async def get_stream_details(self, prov_item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        station_id, track_id = prov_item_id.split("_")
        session = self._get_or_create_session(station_id)
        url = None
        if session.fragments:
            if tracks := session.fragments[-1]:
                for i, track in enumerate(tracks):
                    if track.get("musicId") == track_id:
                        url = track.get("audioURL")
                        break
            # we have started streaming the last song in the fragment.
            session.last_track_started = i >= 3
            self.logger.warning(f"Playing {track.get('songTitle')} {session.last_track_started}")
        if url is None:
            raise MediaNotFoundError("No stream URL found for song.")

        return StreamDetails(
            provider=self.instance_id,
            item_id=prov_item_id,
            audio_format=AudioFormat(
                content_type=ContentType.MP3 if self._use_high_quality() else ContentType.AAC,
            ),
            media_type=MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    async def _get_fragment_data(
        self, session: PandoraStationSession, fragment_index: int
    ) -> list[dict[str, Any]]:
        """Fetch fragment data from Pandora API."""
        # Check if already cached in session
        if fragment_index < len(session.fragments):
            if cached := session.fragments[fragment_index]:
                return cached

        is_stream_start = fragment_index == 0

        fragment_data = {
            "stationId": session.station_id,
            "isStationStart": is_stream_start,
            "fragmentRequestReason": "Normal",
            "audioFormat": "mp3-hifi" if self._use_high_quality() else "aacplus",
            "startingAtTrackId": None,
            "onDemandArtistMessageArtistUidHex": None,
            "onDemandArtistMessageIdHex": None,
        }

        try:
            result: dict[str, Any] = await self._api_request(
                "POST",
                PLAYLIST_FRAGMENT_ENDPOINT,
                data=fragment_data,
                # Mark stream violation retry as already exhausted for non-initial fragments
                # this prevents us from fighting with the concurrent streaming limit
                # if the user starts a stream on a different device while MA is already playing.
                exhausted_retry_reasons=frozenset()
                if is_stream_start
                else frozenset({RETRY_REASON_STREAM_VIOLATION}),
            )
            tracks = []
            for track in result.get("tracks", []):
                if "curator message" not in track.get("songTitle", "").lower():
                    tracks.append(track)

            # Store in session cache
            while len(session.fragments) <= fragment_index:
                session.fragments.append([])
            session.fragments[fragment_index] = tracks
            session.last_track_started = False
            return tracks

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

    def _get_or_create_session(self, station_id: str) -> PandoraStationSession:
        """Get or create a session, with LRU eviction if needed."""
        # Simple LRU: limit to 10 active sessions
        if station_id not in self._sessions and len(self._sessions) >= 10:
            # Remove oldest session
            oldest = min(self._sessions.values(), key=lambda s: s.last_accessed)
            self.logger.debug("Evicting session for station %s", oldest.station_id)
            del self._sessions[oldest.station_id]

        if station_id not in self._sessions:
            self._sessions[station_id] = PandoraStationSession(station_id)

        session = self._sessions[station_id]
        session.last_accessed = time.time()
        return session

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 25,
    ) -> SearchResults:
        """Search library radio stations by name."""
        # Search limited to library stations (API search requires legacy endpoints)
        if MediaType.PLAYLIST not in media_types:
            return SearchResults()

        results: list[Playlist] = []

        async for station in self._get_stations():
            if compare_strings(station.name, search_query):
                results.append(station)
                if len(results) >= limit:
                    break

        return SearchResults(playlists=results)

    async def close(self) -> None:
        """Handle closing of http session if using socks."""
        if self._socks_proxy and self.http_session:
            await self.http_session.close()

    def _use_high_quality(self) -> bool:
        """
        Whether high quality audio should be requested from Pandora.

        This allows a graceful fallback to standard quality if the account is not eligible for
        high-quality streaming, while still respecting the user's preference if they are eligible.
        """
        return self._high_quality_available and self.config.get_value(CONF_QUALITY) == QUALITY_HIGH

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
