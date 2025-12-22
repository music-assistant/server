"""Pandora music provider for Music Assistant."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
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
    AudioFormat,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails, StreamMetadata

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    LOGIN_ENDPOINT,
    PLAYLIST_FRAGMENT_ENDPOINT,
    STATIONS_ENDPOINT,
)
from .helpers import create_auth_headers, get_csrf_token, handle_pandora_error

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class PandoraStationSession:
    """Manages streaming state for a single Pandora station."""

    def __init__(self, station_id: str):
        """Initialize a new station streaming session.

        Args:
            station_id: The Pandora station ID.
        """
        self.station_id = station_id
        self.fragments: list[dict[str, Any] | None] = []
        self.track_map: list[tuple[int, int]] = []
        self.cumulative_times: list[int] = []
        self.last_accessed = time.time()

    def get_track_duration(self, music_track_num: int) -> int:
        """Calculate duration for a specific track index."""
        if not (0 <= music_track_num < len(self.track_map)):
            return 0
        frag_idx, track_idx = self.track_map[music_track_num]
        if frag_idx >= len(self.fragments) or not (frag := self.fragments[frag_idx]):
            return 0
        tracks = frag.get("tracks", [])
        if track_idx >= len(tracks):
            return 0
        return int(tracks[track_idx].get("trackLength", 0))


class PandoraProvider(MusicProvider):
    """Pandora Music Provider."""

    _auth_token: str | None = None
    _user_id: str | None = None
    _csrf_token: str | None = None
    _sessions: dict[str, PandoraStationSession]

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._on_unload_callbacks = []
        self._sessions = {}

        # Authenticate with Pandora
        username = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD))

        await self._authenticate(username, password)

        # Register dynamic stream route
        self._on_unload_callbacks.append(
            self.mass.streams.register_dynamic_route(
                f"/{self.instance_id}_stream", self._handle_stream_request
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for callback in getattr(self, "_on_unload_callbacks", []):
            callback()
        await super().unload(is_removed)

    async def _authenticate(self, username: str, password: str) -> None:
        """Authenticate with Pandora and get auth token."""
        try:
            self._csrf_token = await get_csrf_token(self.mass.http_session)

            login_data = {
                "username": username,
                "password": password,
                "keepLoggedIn": True,
                "existingAuthToken": None,
            }

            headers = create_auth_headers(self._csrf_token)

            async with self.mass.http_session.post(
                LOGIN_ENDPOINT,
                headers=headers,
                json=login_data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    raise LoginFailed(f"Login request failed with status {response.status}")

                response_data = await response.json()
                handle_pandora_error(response_data)

                self._auth_token = response_data.get("authToken")
                if not self._auth_token:
                    raise LoginFailed("No auth token received from Pandora")

                self._user_id = response_data.get("listenerId")
                self.logger.info("Successfully authenticated with Pandora")

        except aiohttp.ClientError as err:
            self.logger.exception("Network error during authentication")
            raise ProviderUnavailableError(
                "Unable to connect to Pandora for authentication"
            ) from err

    async def _api_request(
        self, method: str, url: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make an API request to Pandora."""
        if not self._csrf_token or not self._auth_token:
            raise LoginFailed("Not authenticated with Pandora")

        headers = create_auth_headers(self._csrf_token, self._auth_token)

        try:
            async with self.mass.http_session.request(
                method, url, json=data, headers=headers
            ) as response:
                # Check status BEFORE parsing JSON
                if response.status == 401:
                    raise LoginFailed("Pandora session expired")
                if response.status == 404:
                    raise MediaNotFoundError("Resource not found")
                if response.status >= 500:
                    raise ProviderUnavailableError("Pandora server error")
                if response.status >= 400:
                    raise InvalidDataError(f"Pandora API error: HTTP {response.status}")

                result: dict[str, Any] = await response.json()
                handle_pandora_error(result)
                return result

        except aiohttp.ClientError as err:
            raise ProviderUnavailableError("Unable to connect to Pandora") from err
        except (ValueError, KeyError) as err:
            raise InvalidDataError("Invalid response from Pandora") from err

    @use_cache(3600)
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

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        self.logger.debug("Fetching Pandora stations")

        response = await self._api_request("POST", STATIONS_ENDPOINT, data={})

        stations = response.get("stations", [])
        self.logger.debug("Found %d Pandora stations", len(stations))

        for station in stations:
            station_image = None
            if art := station.get("art"):
                art_url = next(
                    (item["url"] for item in art if item.get("size") == 500),
                    art[-1]["url"] if art else None,
                )
                if art_url:
                    station_image = MediaItemImage(
                        type=ImageType.THUMB,
                        path=art_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
            yield Radio(
                item_id=station["stationId"],
                provider=self.instance_id,
                name=station["name"],
                metadata=MediaItemMetadata(
                    images=UniqueList([station_image]) if station_image else None,
                ),
                provider_mappings={
                    ProviderMapping(
                        item_id=station["stationId"],
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if media_type != MediaType.RADIO:
            raise MediaNotFoundError(f"Unsupported media type: {media_type}")

        # Create playlist with 1000 track placeholders for continuous streaming
        parts = [
            MultiPartPath(
                path=f"{self.mass.streams.base_url}/{self.instance_id}_stream?"
                f"station_id={item_id}&track_num={i}"
            )
            for i in range(1000)
        ]
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.AAC,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=parts,
            can_seek=False,
            allow_seek=False,
            stream_metadata=StreamMetadata(
                title="Pandora Radio",
            ),
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=5,  # Check every 5 seconds
        )

    async def _get_fragment_data(
        self, session: PandoraStationSession, fragment_index: int
    ) -> dict[str, Any]:
        """Fetch fragment data from Pandora API."""
        # Check if already cached in session
        if fragment_index < len(session.fragments):
            cached = session.fragments[fragment_index]
            if cached is not None:
                return cached

        fragment_data = {
            "stationId": session.station_id,
            "isStationStart": fragment_index == 0,
            "fragmentRequestReason": "Normal",
            "audioFormat": "aacplus",
            "startingAtTrackId": None,
            "onDemandArtistMessageArtistUidHex": None,
            "onDemandArtistMessageIdHex": None,
        }

        try:
            result: dict[str, Any] = await self._api_request(
                "POST",
                PLAYLIST_FRAGMENT_ENDPOINT,
                data=fragment_data,
            )

            # Store in session cache
            while len(session.fragments) <= fragment_index:
                session.fragments.append(None)
            session.fragments[fragment_index] = result

            tracks = result.get("tracks", [])

            # Calculate starting cumulative time for this fragment
            if session.cumulative_times:
                # Get the last music track's end time
                last_music_track_num = len(session.track_map) - 1
                last_start = session.cumulative_times[-1]
                last_duration = session.get_track_duration(last_music_track_num)
                current_cumulative = last_start + last_duration
            else:
                current_cumulative = 0

            for track_idx, track in enumerate(tracks):
                title = track.get("songTitle", "")
                # Skip curator messages from the mapping
                if "Curator Message" not in title and "curator message" not in title.lower():
                    session.track_map.append((fragment_index, track_idx))
                    session.cumulative_times.append(current_cumulative)

                    duration = track.get("trackLength", 0)
                    current_cumulative += duration

            return result

        except MediaNotFoundError:
            raise
        except InvalidDataError as err:
            self.logger.error("Invalid fragment data for station %s: %s", session.station_id, err)
            raise

    async def _handle_stream_request(self, request: web.Request) -> web.Response:
        """Handle dynamic stream request.

        Map track numbers to Pandora fragments and redirect to audio URLs.
        """
        if not (station_id := request.query.get("station_id")):
            return web.Response(status=400, text="Missing station_id")
        if not (track_num_str := request.query.get("track_num")):
            return web.Response(status=400, text="Missing track_num")

        try:
            music_track_num = int(track_num_str)
        except ValueError:
            return web.Response(status=400, text="Invalid track_num")

        # Get or create session with LRU eviction
        session = self._get_or_create_session(station_id)

        # If we don't have this music track yet, fetch more fragments
        while music_track_num >= len(session.track_map):
            next_fragment_idx = len(session.fragments)
            await self._get_fragment_data(session, next_fragment_idx)

        # Look up the actual fragment/track position
        fragment_idx, track_idx = session.track_map[music_track_num]

        try:
            # Ensure fragment is loaded
            if fragment_idx >= len(session.fragments) or not session.fragments[fragment_idx]:
                await self._get_fragment_data(session, fragment_idx)

            fragment = session.fragments[fragment_idx]
            if not fragment:
                return web.Response(status=404, text="Track unavailable")

            # Get the track
            tracks = fragment.get("tracks", [])
            if track_idx >= len(tracks):
                self.logger.error(
                    "Track index %d out of range (fragment has %d tracks)",
                    track_idx,
                    len(tracks),
                )
                return web.Response(status=404, text="Track unavailable")

            track = tracks[track_idx]
            audio_url = track.get("audioURL")

            if not audio_url:
                self.logger.error("No audio URL in track data")
                return web.Response(status=404, text="Track unavailable")

            # Redirect to the actual audio URL
            return web.Response(status=302, headers={"Location": audio_url})

        except (MediaNotFoundError, InvalidDataError) as err:
            self.logger.error("Stream error: %s", err)
            return web.Response(status=404, text="Stream unavailable")

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
        if MediaType.RADIO not in media_types:
            return SearchResults()

        results: list[Radio] = []

        async for station in self.get_library_radios():
            if compare_strings(station.name, search_query):
                results.append(station)
                if len(results) >= limit:
                    break

        return SearchResults(radio=results)

    async def _update_stream_metadata(
        self, streamdetails: StreamDetails, elapsed_time: int
    ) -> None:
        """Update stream metadata based on elapsed playback time."""
        station_id = streamdetails.item_id

        # Get session if it exists
        if station_id not in self._sessions:
            return

        session = self._sessions[station_id]
        session.last_accessed = time.time()

        if not session.track_map or not session.cumulative_times:
            return

        # Find the current track based on elapsed time
        current_track_idx = None
        for i, start_time in enumerate(session.cumulative_times):
            # Calculate when this track ends
            if i + 1 < len(session.cumulative_times):
                end_time = session.cumulative_times[i + 1]
            else:
                end_time = start_time + session.get_track_duration(i)

            if start_time <= elapsed_time < end_time:
                current_track_idx = i
                break

        if current_track_idx is None:
            return

        # Get track data
        frag_idx, track_idx = session.track_map[current_track_idx]
        if frag_idx >= len(session.fragments):
            return
        fragment = session.fragments[frag_idx]
        if not fragment:
            return

        tracks = fragment.get("tracks", [])
        if track_idx >= len(tracks):
            return

        track = tracks[track_idx]

        # Update metadata if title changed
        if not streamdetails.stream_metadata or streamdetails.stream_metadata.title == track.get(
            "songTitle"
        ):
            return

        # Get album art
        album_art_url = None
        if album_art := track.get("albumArt"):
            album_art_url = next(
                (art["url"] for art in album_art if art.get("size") == 500),
                album_art[-1]["url"] if album_art else None,
            )

        streamdetails.stream_metadata.title = track.get("songTitle", "Unknown Song")
        streamdetails.stream_metadata.artist = track.get("artistName", "Unknown Artist")
        streamdetails.stream_metadata.album = track.get("albumTitle")
        streamdetails.stream_metadata.image_url = album_art_url
        streamdetails.stream_metadata.duration = track.get("trackLength")
        streamdetails.stream_metadata.uri = track.get("songDetailURL")
