"""Pandora radio provider with single track streaming."""

from __future__ import annotations

import asyncio
import json
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
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, UnplayableMediaError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.helpers.util import lock
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    CONF_PASSWORD,
    CONF_USERNAME,
    LOGIN_ENDPOINT,
    PLAYLIST_FRAGMENT_ENDPOINT,
    STATIONS_ENDPOINT,
)
from .helpers import create_auth_headers, get_csrf_token, handle_pandora_error


class PandoraProvider(MusicProvider):
    """Implementation of a Pandora Radio Provider with single track streaming."""

    _auth_token: str | None = None
    _csrf_token: str | None = None
    _user_profile: dict[str, Any] | None = None
    _station_fragments: dict[str, dict[str, Any]] = {}
    _station_track_positions: dict[str, int] = {}  # Track position in fragment per station

    throttler: ThrottlerManager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.throttler = ThrottlerManager(rate_limit=10, period=60)

        try:
            await self.login()

        except LoginFailed as e:
            self.logger.error("Authentication failed: %s", e)
            raise
        except Exception as e:
            self.logger.error("Failed to initialize Pandora provider: %s", e)
            raise

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        try:
            stations_data = await self._api_request("POST", STATIONS_ENDPOINT, data={})
            stations = stations_data.get("stations", [])

            for station_data in stations:
                try:
                    yield self._parse_radio(station_data)
                except Exception as e:
                    self.logger.debug("Failed to parse station: %s", e)

        except Exception as e:
            self.logger.error("Failed to retrieve stations: %s", e)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        try:
            stations_data = await self._api_request("POST", STATIONS_ENDPOINT, data={})
            stations = stations_data.get("stations", [])

            for station_data in stations:
                if str(station_data.get("stationId")) == prov_radio_id:
                    return self._parse_radio(station_data)

            raise MediaNotFoundError(f"Radio station {prov_radio_id} not found")

        except Exception as e:
            self.logger.error("Failed to get radio station %s: %s", prov_radio_id, e)
            raise MediaNotFoundError(f"Radio station {prov_radio_id} not found") from e

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station using custom streaming."""
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")

        # Fixed to HIGH quality (192 kbps AAC+)
        content_type = ContentType.AAC
        bit_rate = 192

        # Get initial metadata
        stream_metadata = None
        try:
            fragment_data = await self._get_station_fragment(item_id, is_start=True)
            if fragment_data and fragment_data.get("tracks"):
                current_position = self._station_track_positions.get(item_id, 0)
                if current_position < len(fragment_data["tracks"]):
                    current_track = fragment_data["tracks"][current_position]
                    stream_metadata = StreamMetadata(
                        title=current_track.get("songTitle", "Unknown Title"),
                        artist=current_track.get("artistName"),
                        album=current_track.get("albumTitle"),
                        duration=int(current_track.get("trackLength", 0) * 1000)
                        if current_track.get("trackLength")
                        else None,
                    )
        except Exception as e:
            self.logger.debug("Failed to get initial metadata for %s: %s", item_id, e)

        return StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=bit_rate,
                channels=2,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.CUSTOM,
            allow_seek=False,
            can_seek=False,
            duration=0,  # Infinite radio stream
            stream_metadata=stream_metadata,
        )

    async def get_audio_stream(  # noqa: PLR0915
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Stream continuous radio with proper timing."""
        station_id = streamdetails.item_id

        # Calculate streaming parameters for HIGH quality (192 kbps AAC+)
        target_bitrate = 192  # kbps
        bytes_per_second = (target_bitrate * 1000) / 8  # Convert to bytes/second

        self.logger.info("Starting continuous radio stream for station %s", station_id)

        try:
            while True:  # Infinite radio loop
                # Get current fragment and position
                fragment_data = self._station_fragments.get(station_id)
                current_position = self._station_track_positions.get(station_id, 0)

                # Get new fragment if needed
                if not fragment_data or not fragment_data.get("tracks"):
                    fragment_data = await self._get_station_fragment(station_id, is_start=True)
                    current_position = 0
                    self._station_track_positions[station_id] = 0

                # Check if we need a new fragment (position beyond current tracks)
                if fragment_data and current_position >= len(fragment_data.get("tracks", [])):
                    fragment_data = await self._get_station_fragment(station_id, is_start=False)
                    current_position = 0
                    self._station_track_positions[station_id] = 0

                if not fragment_data or not fragment_data.get("tracks"):
                    self.logger.error("No tracks available for station %s", station_id)
                    await asyncio.sleep(5)
                    continue

                tracks = fragment_data["tracks"]
                if current_position >= len(tracks):
                    self.logger.error(
                        "Track position %s beyond available tracks for station %s",
                        current_position,
                        station_id,
                    )
                    await asyncio.sleep(5)
                    continue

                # Get the current track
                track_data = tracks[current_position]
                audio_url = track_data.get("audioURL")

                if not audio_url:
                    self.logger.warning(
                        "No audio URL for track at position %s in station %s",
                        current_position,
                        station_id,
                    )
                    # Add silence for missing tracks
                    silence_duration = 10  # seconds
                    silence_bytes_total = int(bytes_per_second * silence_duration)
                    chunk_size = 8192

                    for i in range(0, silence_bytes_total, chunk_size):
                        chunk = b"\x00" * min(chunk_size, silence_bytes_total - i)
                        yield chunk
                        await asyncio.sleep(chunk_size / bytes_per_second)

                    self._station_track_positions[station_id] = current_position + 1
                    continue

                track_info = (
                    f"{track_data.get('artistName', 'Unknown')} - "
                    f"{track_data.get('songTitle', 'Unknown')}"
                )
                track_duration = track_data.get("trackLength", 0)  # seconds

                self.logger.info("Now streaming: %s (%.1f seconds)", track_info, track_duration)

                try:
                    track_start_time = time.time()

                    async with self.mass.http_session.get(audio_url) as response:
                        if response.status != 200:
                            self.logger.error(
                                "Failed to get audio for %s, status: %s",
                                track_info,
                                response.status,
                            )
                            # Add silence for failed tracks
                            silence_duration = 10
                            silence_bytes_total = int(bytes_per_second * silence_duration)
                            chunk_size = 8192

                            for i in range(0, silence_bytes_total, chunk_size):
                                chunk = b"\x00" * min(chunk_size, silence_bytes_total - i)
                                yield chunk
                                await asyncio.sleep(chunk_size / bytes_per_second)

                            self._station_track_positions[station_id] = current_position + 1
                            continue

                        # Stream with simple chunk-based timing
                        async for chunk in response.content.iter_chunked(8192):
                            if not chunk:
                                break

                            yield chunk

                            # Sleep for the time this chunk should take to play
                            chunk_play_time = len(chunk) / bytes_per_second
                            await asyncio.sleep(chunk_play_time)

                            # Check if we've been streaming long enough
                            elapsed = time.time() - track_start_time
                            if elapsed >= (track_duration - 0.1):
                                self.logger.debug("Ending track after %.1f seconds", elapsed)
                                break

                    # Track completed
                    actual_duration = time.time() - track_start_time
                    self.logger.info(
                        "Completed streaming %s in %.1f seconds (expected: %.1f)",
                        track_info,
                        actual_duration,
                        track_duration,
                    )

                except asyncio.CancelledError:
                    self.logger.info("Stream cancelled for %s", track_info)
                    raise
                except Exception as e:
                    self.logger.error("Error streaming %s: %s", track_info, e)
                    # Add silence for error recovery
                    silence_duration = 5
                    silence_bytes_total = int(bytes_per_second * silence_duration)
                    chunk_size = 8192

                    for i in range(0, silence_bytes_total, chunk_size):
                        chunk = b"\x00" * min(chunk_size, silence_bytes_total - i)
                        yield chunk
                        await asyncio.sleep(chunk_size / bytes_per_second)

                # Move to next track
                self._station_track_positions[station_id] = current_position + 1

        except asyncio.CancelledError:
            self.logger.info("Radio stream cancelled for station %s", station_id)
            raise
        except Exception as e:
            self.logger.error("Error in radio stream for station %s: %s", station_id, e)
            raise

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        return [radio async for radio in self.get_library_radios()]

    def _parse_radio(self, station_data: dict[str, Any]) -> Radio:
        """Create a Radio object from station data."""
        station_id = str(station_data.get("stationId", ""))
        station_name = station_data.get("name", "Unknown Station")

        radio = Radio(
            provider=self.lookup_key,
            item_id=station_id,
            name=station_name,
            provider_mappings={
                ProviderMapping(
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    item_id=station_id,
                    available=True,
                )
            },
        )

        # Add station artwork if available
        if art_list := station_data.get("art"):
            if isinstance(art_list, list) and art_list:
                best_art = max(art_list, key=lambda x: x.get("size", 0))
                radio.metadata.add_image(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=best_art["url"],
                        provider=self.lookup_key,
                        remotely_accessible=True,
                    )
                )

        return radio

    async def _get_station_fragment(
        self, station_id: str, is_start: bool = False
    ) -> dict[str, Any] | None:
        """Get a fragment of tracks from a station."""
        fragment_data = {
            "stationId": station_id,
            "isStationStart": is_start,
            "fragmentRequestReason": "Normal",
            "audioFormat": "aacplus",
            "startingAtTrackId": None,
            "onDemandArtistMessageArtistUidHex": None,
            "onDemandArtistMessageIdHex": None,
        }

        try:
            result = await self._api_request("POST", PLAYLIST_FRAGMENT_ENDPOINT, data=fragment_data)
            # Cache the fragment for this station
            self._station_fragments[station_id] = result
            return result
        except Exception as e:
            self.logger.error("Failed to get fragment for station %s: %s", station_id, e)
            return None

    @lock
    async def login(self, force_refresh: bool = False) -> None:
        """Authenticate with Pandora."""
        if not force_refresh and self._auth_token:
            return

        username = self.config.get_value(CONF_USERNAME)
        password = self.config.get_value(CONF_PASSWORD)

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
                data=json.dumps(login_data),
                ssl=True,
            ) as response:
                if response.status != 200:
                    raise LoginFailed(f"Login request failed with status {response.status}")

                response_data = await response.json()
                handle_pandora_error(response_data)

                self._auth_token = response_data.get("authToken")
                if not self._auth_token:
                    raise LoginFailed("No auth token received from Pandora")

                self._user_profile = {
                    "username": response_data.get("username", username),
                    "listenerId": response_data.get("listenerId"),
                }

                self.logger.info(
                    "Successfully logged in to Pandora as %s", self._user_profile["username"]
                )

        except LoginFailed:
            raise
        except Exception as e:
            self.logger.error("Login failed: %s", e)
            raise LoginFailed(f"Authentication failed: {e}") from e

    @throttle_with_retries
    async def _api_request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make authenticated API request to Pandora."""

        def get_auth_headers() -> dict[str, str]:
            if not self._auth_token or not self._csrf_token:
                raise LoginFailed("Authentication failed - tokens are missing.")
            return create_auth_headers(self._csrf_token, self._auth_token)

        async def perform_request(headers: dict[str, str]) -> aiohttp.ClientResponse:
            request_kwargs: dict[str, Any] = {
                "headers": headers,
                "ssl": True,
            }
            if data is not None:
                request_kwargs["data"] = json.dumps(data)
            if params is not None:
                request_kwargs["params"] = params
            return await self.mass.http_session.request(method, endpoint, **request_kwargs)

        if not self._auth_token or not self._csrf_token:
            await self.login()

        try:
            async with await perform_request(get_auth_headers()) as response:
                if response.status == 401:
                    await self.login(force_refresh=True)
                    async with await perform_request(get_auth_headers()) as retry_response:
                        if retry_response.status != 200:
                            error_text = await retry_response.text()
                            self.logger.error(
                                "API request failed with status %s: %s",
                                retry_response.status,
                                error_text,
                            )
                            raise aiohttp.ClientError(
                                f"API request failed with status {retry_response.status}: "
                                f"{error_text}"
                            )
                        response_data: dict[str, Any] = await retry_response.json()
                elif response.status != 200:
                    error_text = await response.text()
                    self.logger.error(
                        "API request failed with status %s: %s", response.status, error_text
                    )
                    raise aiohttp.ClientError(
                        f"API request failed with status {response.status}: {error_text}"
                    )
                else:
                    response_data = await response.json()

            handle_pandora_error(response_data)
            return response_data
        except aiohttp.ClientError:
            raise
        except Exception as e:
            self.logger.error("API request failed: %s", e)
            raise aiohttp.ClientError(f"Request failed: {e}") from e
