"""Pandora radio provider."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import aiohttp
from aiohttp import web
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
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
from music_assistant.helpers.util import lock, select_free_port
from music_assistant.helpers.webserver import Webserver
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    AUDIO_QUALITIES,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_AUDIO_QUALITY,
    LOGIN_ENDPOINT,
    PLAYLIST_FRAGMENT_ENDPOINT,
    STATIONS_ENDPOINT,
)
from .helpers import create_auth_headers, get_csrf_token, handle_pandora_error


class PandoraProvider(MusicProvider):
    """Implementation of a Pandora Radio Provider with sequential FFmpeg streaming."""

    _auth_token: str | None = None
    _csrf_token: str | None = None
    _user_profile: dict[str, Any] | None = None
    _station_fragments: dict[str, dict[str, Any]] = {}

    # Proxy server components
    _proxy_server: Webserver | None = None
    _proxy_port: int | None = None
    _active_streams: dict[str, asyncio.Task[None]] = {}  # station_id -> streaming task
    _current_stream_details: dict[str, StreamDetails] = {}  # station_id -> StreamDetails

    throttler: ThrottlerManager

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.throttler = ThrottlerManager(rate_limit=10, period=60)

        try:
            await self.login()
            await self._setup_proxy_server()
        except LoginFailed as e:
            self.logger.error("Authentication failed: %s", e)
            raise
        except Exception as e:
            self.logger.error("Failed to initialize Pandora provider: %s", e)
            raise

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Cancel all active streaming tasks
        for task in self._active_streams.values():
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self._active_streams.clear()
        self._current_stream_details.clear()

        # Close proxy server
        if self._proxy_server:
            await self._proxy_server.close()

    async def _setup_proxy_server(self) -> None:
        """Set up the local proxy server for streaming."""
        bind_ip = "127.0.0.1"
        self._proxy_port = await select_free_port(8100, 9999)

        self._proxy_server = Webserver(self.logger)

        # Define the streaming endpoint
        async def stream_handler(request: web.Request) -> web.StreamResponse:
            return await self._handle_stream_request(request)

        await self._proxy_server.setup(
            bind_ip=bind_ip,
            bind_port=self._proxy_port,
            base_url=f"{bind_ip}:{self._proxy_port}",
            static_routes=[
                ("GET", "/pandora/{station_id}.mp3", stream_handler),
            ],
        )

        self.logger.debug(f"Pandora proxy server running at {bind_ip}:{self._proxy_port}")

    async def _handle_stream_request(self, request: web.Request) -> web.StreamResponse:
        """Handle a streaming request for a station."""
        station_id = request.match_info["station_id"]

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Transfer-Encoding": "chunked",
                "Accept-Ranges": "none",
            },
        )
        await response.prepare(request)

        try:
            # Start the FFmpeg streaming task for this station if not already running
            if station_id not in self._active_streams:
                self._active_streams[station_id] = self.mass.create_task(
                    self._stream_directly(station_id, response)
                )

            # Wait for the streaming task to complete
            await self._active_streams[station_id]

        except Exception as e:
            self.logger.error("Error in stream handler for station %s: %s", station_id, e)
        finally:
            # Clean up
            if station_id in self._active_streams:
                del self._active_streams[station_id]

        return response

    async def _stream_directly(self, station_id: str, response: web.StreamResponse) -> None:  # noqa: PLR0915
        """Stream tracks directly in a continuous loop with larger initial buffer."""
        self.logger.info("Starting continuous streaming for station %s", station_id)

        last_track_id = None

        try:
            fragment_data = await self._get_station_fragment(station_id, is_start=True)
            if not fragment_data or not fragment_data.get("tracks"):
                self.logger.warning("No initial tracks found for station %s", station_id)
                return

            while True:
                tracks = fragment_data.get("tracks", []) if fragment_data else []

                if not tracks:
                    self.logger.debug("End of fragment, fetching next one.")
                    fragment_data = await self._get_station_fragment(
                        station_id, last_track_id=last_track_id
                    )
                    tracks = (
                        fragment_data.get("tracks", [])
                        if fragment_data and fragment_data.get("tracks")
                        else []
                    )
                    if not tracks:
                        self.logger.warning("Could not fetch new fragment, ending stream.")
                        break

                track_data = tracks.pop(0)
                audio_url = track_data.get("audioURL")

                if not audio_url:
                    self.logger.warning("Track has no audio URL, skipping.")
                    continue

                self.logger.info(
                    "Now streaming: %s - %s",
                    track_data.get("artistName"),
                    track_data.get("songTitle"),
                )
                last_track_id = track_data.get("trackId")

                await self._update_stream_metadata(station_id, track_data)

                try:
                    self.logger.debug("Attempting to connect to audio URL: %s", audio_url)
                    async with self.mass.http_session.get(audio_url) as track_response:
                        self.logger.debug(
                            "Connected to audio URL with status: %s", track_response.status
                        )
                        if track_response.status != 200:
                            self.logger.error(
                                "Failed to fetch track audio from %s: status %d",
                                audio_url,
                                track_response.status,
                            )
                            continue

                        # Read a much larger chunk to handle the client's aggressive read timeout.
                        self.logger.debug("Reading first chunk for pre-buffering (256 KB).")
                        first_chunk = await track_response.content.read(262144)
                        if first_chunk:
                            self.logger.debug(
                                "Writing first chunk of %d bytes to stream.", len(first_chunk)
                            )
                            await response.write(first_chunk)
                            self.logger.debug("Successfully wrote first chunk.")

                        total_bytes_sent = len(first_chunk)

                        self.logger.debug("Starting continuous stream of remaining chunks.")
                        async for chunk in track_response.content.iter_chunked(8192):
                            await response.write(chunk)
                            total_bytes_sent += len(chunk)
                            self.logger.debug(
                                "Wrote a chunk, total bytes sent: %d", total_bytes_sent
                            )

                except (aiohttp.ClientError, ConnectionResetError) as e:
                    self.logger.error("Error fetching or writing track audio: %s", e)
                    self.logger.debug("Breaking streaming loop due to connection error.")
                    break

        except Exception as e:
            self.logger.error(
                "Error in continuous streaming loop for station %s: %s", station_id, e
            )
        finally:
            self.logger.info("Stopping continuous stream for station %s", station_id)
            try:
                await response.write_eof()
            except (ConnectionResetError, RuntimeError):
                self.logger.debug("Stream transport was already closed, nothing to do.")
            except Exception as e:
                self.logger.error("Error writing EOF to stream: %s", e)

    async def _update_stream_metadata(self, station_id: str, track_data: dict[str, Any]) -> None:
        """Update stream metadata for the current track."""
        if station_id not in self._current_stream_details:
            return

        stream_details = self._current_stream_details[station_id]

        # Create metadata for current track
        title = track_data.get("songTitle", "Unknown Title")
        artist = track_data.get("artistName", "Unknown Artist")
        album = track_data.get("albumTitle")

        # Get album art
        image_url = None
        if album_art := track_data.get("albumArt"):
            if isinstance(album_art, list) and album_art:
                best_art = max(album_art, key=lambda x: x.get("size", 0))
                image_url = best_art.get("url")

        # Get duration
        duration = None
        if track_length := track_data.get("trackLength"):
            duration = int(track_length * 1000)  # Convert to milliseconds

        stream_metadata = StreamMetadata(
            title=title,
            artist=artist,
            album=album,
            image_url=image_url,
            duration=duration,
        )

        stream_details.stream_metadata = stream_metadata
        self.logger.debug("Updated metadata for station %s: %s - %s", station_id, artist, title)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_RADIOS,
        }

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a postfix for the instance name."""
        if self._user_profile:
            username = self._user_profile.get("username")
            return str(username) if username is not None else None
        return None

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
        """Get streamdetails for a radio station."""
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")

        # Create proxy URL for this station
        proxy_url = f"http://127.0.0.1:{self._proxy_port}/pandora/{item_id}.mp3"

        # Get audio quality from config
        quality_setting = self.config.get_value("audio_quality", DEFAULT_AUDIO_QUALITY)
        if not isinstance(quality_setting, str):
            quality_setting = DEFAULT_AUDIO_QUALITY

        audio_quality = AUDIO_QUALITIES.get(quality_setting, AUDIO_QUALITIES[DEFAULT_AUDIO_QUALITY])
        content_type = ContentType.MP3  # Always MP3 output from FFmpeg

        bitrate_value = audio_quality["bitrate"]
        if isinstance(bitrate_value, int):
            bit_rate = bitrate_value
        elif isinstance(bitrate_value, (float, str)):
            bit_rate = int(bitrate_value)
        else:
            bit_rate = 128

        # Get initial metadata from first fragment
        stream_metadata = None
        try:
            fragment_data = await self._get_station_fragment(item_id, is_start=True)
            if fragment_data and fragment_data.get("tracks"):
                first_track = fragment_data["tracks"][0]
                stream_metadata = StreamMetadata(
                    title=first_track.get("songTitle", "Unknown Title"),
                    artist=first_track.get("artistName"),
                    album=first_track.get("albumTitle"),
                    duration=int(first_track.get("trackLength", 0) * 1000)
                    if first_track.get("trackLength")
                    else None,
                )

                if album_art := first_track.get("albumArt"):
                    if isinstance(album_art, list) and album_art:
                        best_art = max(album_art, key=lambda x: x.get("size", 0))
                        stream_metadata.image_url = best_art.get("url")

        except Exception as e:
            self.logger.debug("Failed to get initial metadata for %s: %s", item_id, e)

        stream_details = StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=bit_rate,
                channels=2,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,  # Use HTTP, not CUSTOM
            path=proxy_url,  # Direct URL to proxy
            allow_seek=False,
            can_seek=False,
            duration=0,  # Radio streams are infinite
            stream_metadata=stream_metadata,
        )

        # Store reference for metadata updates
        self._current_stream_details[item_id] = stream_details

        return stream_details

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
        self,
        station_id: str,
        is_start: bool = False,
        last_track_id: str | None = None,  # 👈 Add this new parameter
    ) -> dict[str, Any] | None:
        """Get a fragment of tracks from a station."""
        fragment_data = {
            "stationId": station_id,
            "isStationStart": is_start,
            "fragmentRequestReason": "Normal",
            "audioFormat": "aacplus",
            "startingAtTrackId": last_track_id,  # 👈 Use the new parameter here
            "onDemandArtistMessageArtistUidHex": None,
            "onDemandArtistMessageIdHex": None,
        }

        try:
            return await self._api_request("POST", PLAYLIST_FRAGMENT_ENDPOINT, data=fragment_data)
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
