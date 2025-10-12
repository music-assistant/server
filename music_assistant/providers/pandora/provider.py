"""Pandora radio provider for Music Assistant."""

from __future__ import annotations

from collections import OrderedDict
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
    LoginFailed,
    MediaNotFoundError,
    ProviderUnavailableError,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
)
from music_assistant_models.streamdetails import MultiPartPath, StreamDetails

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
    """Pandora Radio provider for Music Assistant.

    Provides access to Pandora radio stations with streaming support.
    Stations must be created through the Pandora website or mobile app first.

    Note: This provider uses Pandora's REST API and only supports radio streaming.
    Search and station creation require the GraphQL API which is not implemented.
    """

    _auth_token: str | None = None
    _csrf_token: str | None = None
    _user_profile: dict[str, Any] | None = None
    _station_fragments: OrderedDict[str, dict[str, Any]]

    throttler: ThrottlerManager

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)
        self._station_fragments = OrderedDict()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.throttler = ThrottlerManager(rate_limit=10, period=60)
        await self.login()

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider.

        Yields:
            Radio objects for each station in the user's library
        """
        try:
            stations_data = await self._api_request("POST", STATIONS_ENDPOINT, data={})
            stations = stations_data.get("stations", [])

            for station_data in stations:
                try:
                    yield self._parse_radio(station_data)
                except (KeyError, ValueError, TypeError) as e:
                    self.logger.debug("Failed to parse station: %s", e)

        except (aiohttp.ClientError, ProviderUnavailableError, LoginFailed) as e:
            self.logger.error("Failed to retrieve stations: %s", e)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id.

        Args:
            prov_radio_id: Provider-specific radio station ID

        Returns:
            Radio object with full details

        Raises:
            MediaNotFoundError: If station not found
        """
        try:
            stations_data = await self._api_request("POST", STATIONS_ENDPOINT, data={})
            stations = stations_data.get("stations", [])

            for station_data in stations:
                if str(station_data.get("stationId")) == prov_radio_id:
                    return self._parse_radio(station_data)

            raise MediaNotFoundError(f"Radio station {prov_radio_id} not found")

        except MediaNotFoundError:
            raise
        except (aiohttp.ClientError, ProviderUnavailableError, LoginFailed) as e:
            self.logger.error("Failed to get radio station %s: %s", prov_radio_id, e)
            raise MediaNotFoundError(f"Radio station {prov_radio_id} not found") from e

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for a radio station.

        Builds a multi-file playlist by fetching multiple fragments from Pandora,
        creating approximately 2-3 hours of continuous playback.

        Args:
            item_id: Station ID
            media_type: Must be MediaType.RADIO

        Returns:
            StreamDetails with multi-part playlist

        Raises:
            UnplayableMediaError: If media type unsupported or no tracks available
        """
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")

        # Fixed to HIGH quality (192 kbps AAC+)
        content_type = ContentType.AAC
        bit_rate = 192

        parts = []
        total_duration = 0
        max_fragments = 10

        try:
            # Fetch fragments
            for fragment_count in range(max_fragments):
                is_start = fragment_count == 0
                fragment_data = await self._get_station_fragment(item_id, is_start=is_start)

                if not fragment_data or not fragment_data.get("tracks"):
                    self.logger.debug(
                        "No more fragments available after %d fragments", fragment_count
                    )
                    break

                for track in fragment_data["tracks"]:
                    audio_url = track.get("audioURL")
                    track_duration = track.get("trackLength", 0)

                    if audio_url and track_duration:
                        parts.append(MultiPartPath(path=audio_url, duration=track_duration))
                        total_duration += track_duration

            self.logger.info(
                "Built radio playlist for station %s: %d tracks, %.1f minutes total",
                item_id,
                len(parts),
                total_duration / 60,
            )

        except (aiohttp.ClientError, ProviderUnavailableError, LoginFailed) as e:
            self.logger.error("Failed to build radio playlist for %s: %s", item_id, e)
            raise UnplayableMediaError(f"Could not build radio playlist: {e}") from e

        if not parts:
            raise UnplayableMediaError(f"No tracks available for station {item_id}")

        return StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
            path=parts,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=bit_rate,
                channels=2,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            allow_seek=False,
            can_seek=False,
            duration=int(total_duration),
        )

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items.

        Args:
            path: Browse path (unused, returns all stations)

        Returns:
            List of all radio stations
        """
        return [radio async for radio in self.get_library_radios()]

    def _parse_radio(self, station_data: dict[str, Any]) -> Radio:
        """Create a Radio object from Pandora station data.

        Args:
            station_data: Raw station data from Pandora API

        Returns:
            Radio object
        """
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
        """Get a fragment of tracks from a station.

        Args:
            station_id: Pandora station ID
            is_start: Whether this is the first fragment for the station

        Returns:
            Fragment data with tracks, or None if request fails
        """
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

            # Cache the fragment with LRU behavior
            self._station_fragments[station_id] = result
            if len(self._station_fragments) > 50:
                self._station_fragments.popitem(last=False)

            return result
        except (aiohttp.ClientError, ProviderUnavailableError) as e:
            self.logger.warning("Failed to get fragment for station %s: %s", station_id, e)
            return None

    @lock
    async def login(self, force_refresh: bool = False) -> None:
        """Authenticate with Pandora.

        Args:
            force_refresh: Force re-authentication even if token exists

        Raises:
            LoginFailed: If authentication fails
        """
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

                self._user_profile = {
                    "username": response_data.get("username", username),
                    "listenerId": response_data.get("listenerId"),
                }

                self.logger.info(
                    "Successfully logged in to Pandora as %s", self._user_profile["username"]
                )

        except (ResourceTemporarilyUnavailable, aiohttp.ClientError) as e:
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
        """Make authenticated API request to Pandora.

        Handles authentication token refresh on 401 errors.

        Args:
            method: HTTP method
            endpoint: API endpoint URL
            data: Optional request body data
            params: Optional query parameters

        Returns:
            JSON response data

        Raises:
            LoginFailed: If authentication fails
            ProviderUnavailableError: If request fails
        """
        if not self._auth_token or not self._csrf_token:
            await self.login()

        # After login, tokens should be set
        if not self._auth_token or not self._csrf_token:
            raise LoginFailed("Authentication failed - tokens are missing after login")

        # Build headers
        headers = create_auth_headers(self._csrf_token, self._auth_token)

        # Build request kwargs
        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": aiohttp.ClientTimeout(total=30),
        }
        if data is not None:
            request_kwargs["json"] = data
        if params is not None:
            request_kwargs["params"] = params

        try:
            # First attempt
            async with self.mass.http_session.request(
                method, endpoint, **request_kwargs
            ) as response:
                if response.status == 401:
                    # Token expired, refresh and retry once
                    self.logger.debug("Auth token expired, refreshing...")
                    await self.login(force_refresh=True)

                    if not self._auth_token or not self._csrf_token:
                        raise LoginFailed("Authentication failed - tokens missing after refresh")

                    request_kwargs["headers"] = create_auth_headers(
                        self._csrf_token, self._auth_token
                    )

                    # Retry request
                    async with self.mass.http_session.request(
                        method, endpoint, **request_kwargs
                    ) as retry_response:
                        if retry_response.status != 200:
                            self.logger.error(
                                "API request failed after retry with status %s",
                                retry_response.status,
                            )
                            raise ProviderUnavailableError(
                                f"API request failed with status {retry_response.status}"
                            )
                        response_data: dict[str, Any] = await retry_response.json()

                elif response.status != 200:
                    self.logger.error("API request failed with status %s", response.status)
                    raise ProviderUnavailableError(
                        f"API request failed with status {response.status}"
                    )
                else:
                    response_data = await response.json()

            handle_pandora_error(response_data)
            return response_data

        except (
            LoginFailed,
            ProviderUnavailableError,
            MediaNotFoundError,
            ResourceTemporarilyUnavailable,
        ):
            raise
        except aiohttp.ClientError as e:
            self.logger.error("Network error during API request: %s", e)
            raise ResourceTemporarilyUnavailable(f"Network error: {e}") from e
        except (KeyError, ValueError) as e:
            self.logger.error("Invalid API response: %s", e)
            raise ProviderUnavailableError(f"Invalid API response: {e}") from e
