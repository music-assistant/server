"""Pandora music provider for Music Assistant."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import web
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
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

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.compare import compare_strings
from music_assistant.models.music_provider import MusicProvider

from .helpers import create_auth_headers, get_csrf_token, handle_pandora_error

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# API Configuration
API_BASE = "https://www.pandora.com/api/v1"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# Pandora returns ~4 tracks per fragment
TRACKS_PER_FRAGMENT = 4

SUPPORTED_FEATURES = (
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.BROWSE,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = PandoraProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.SECURE_STRING,
            label="Username",
            required=True,
            description="Your Pandora account email address",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
            description="Your Pandora account password",
        ),
    )


class PandoraProvider(MusicProvider):
    """Pandora Music Provider."""

    _auth_token: str | None = None
    _user_id: str | None = None
    _csrf_token: str | None = None
    _on_unload_callbacks: list[Callable[[], None]]

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._on_unload_callbacks = []

        # Authenticate with Pandora
        username = str(self.config.get_value(CONF_USERNAME))
        password = str(self.config.get_value(CONF_PASSWORD))

        try:
            await self._authenticate(username, password)
        except Exception as err:
            raise LoginFailed(f"Failed to authenticate with Pandora: {err}") from err

        # Register dynamic stream route
        self._on_unload_callbacks.append(
            self.mass.streams.register_dynamic_route(
                f"/{self.instance_id}_stream", self._handle_stream_request
            )
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for callback in self._on_unload_callbacks:
            callback()
        await super().unload(is_removed)

    async def _authenticate(self, username: str, password: str) -> None:
        """Authenticate with Pandora and get auth token."""
        try:
            # First, get CSRF token
            self._csrf_token = await get_csrf_token(self.mass.http_session)

            # Prepare login data
            login_data = {
                "username": username,
                "password": password,
                "keepLoggedIn": True,
                "existingAuthToken": None,
            }

            # Create headers with CSRF token
            headers = create_auth_headers(self._csrf_token)

            async with self.mass.http_session.post(
                f"{API_BASE}/auth/login",
                headers=headers,
                json=login_data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    raise LoginFailed(f"Login request failed with status {response.status}")

                response_data: dict[str, Any] = await response.json()
                handle_pandora_error(response_data)

                self._auth_token = response_data.get("authToken")
                if not self._auth_token:
                    raise LoginFailed("No auth token received from Pandora")

                self._user_id = response_data.get("listenerId")
                self.logger.info("Successfully authenticated with Pandora")

        except LoginFailed:
            raise
        except Exception as err:
            self.logger.error("Authentication failed: %s", err)
            raise LoginFailed(f"Failed to authenticate with Pandora: {err}") from err

    async def _api_request(
        self,
        method: str,
        url: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an API request to Pandora."""
        if not self._csrf_token or not self._auth_token:
            raise LoginFailed("Not authenticated with Pandora")

        headers = create_auth_headers(self._csrf_token, self._auth_token)

        async with self.mass.http_session.request(
            method,
            url,
            json=data,
            headers=headers,
        ) as response:
            response.raise_for_status()
            result: dict[str, Any] = await response.json()
            handle_pandora_error(result)
            return result

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        self.logger.debug("Fetching Pandora stations")

        try:
            response = await self._api_request(
                "POST",
                f"{API_BASE}/station/getStations",
                data={},
            )
        except Exception as err:
            self.logger.error("Failed to fetch stations: %s", err)
            return

        stations = response.get("stations", [])
        self.logger.debug("Found %d Pandora stations", len(stations))

        for station in stations:
            # Get station artwork (prefer 500px version)
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
                    provider=self.domain,
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

    @use_cache(3600)
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get single radio station details."""
        # For now, just return basic info
        # We could enhance this with more details from the API
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

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if media_type != MediaType.RADIO:
            raise MediaNotFoundError(f"Unsupported media type: {media_type}")

        # Create 1000 dummy tracks pointing to our dynamic handler
        # This provides roughly two days of radio streaming assuming 3 mins per track
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
                title="Pandora",
            ),
        )

    # @use_cache(300)  # Cache fragments for 5 minutes
    async def _get_fragment_data(self, station_id: str, fragment_index: int) -> dict[str, Any]:
        """Fetch fragment data from Pandora API (cached per station + fragment index)."""
        self.logger.debug(
            "Fetching fragment %d for station %s",
            fragment_index,
            station_id,
        )

        fragment_data = {
            "stationId": station_id,
            "isStationStart": fragment_index == 0,
            "fragmentRequestReason": "Normal",
            "audioFormat": "aacplus",
            "startingAtTrackId": None,
            "onDemandArtistMessageArtistUidHex": None,
            "onDemandArtistMessageIdHex": None,
        }

        try:
            return await self._api_request(
                "POST",
                f"{API_BASE}/playlist/getFragment",
                data=fragment_data,
            )
        except Exception as err:
            self.logger.error(
                "Failed to fetch fragment %d for station %s: %s",
                fragment_index,
                station_id,
                err,
            )
            raise

    async def _handle_stream_request(self, request: web.Request) -> web.Response:
        """
        Handle dynamic stream request.

        This is called by FFmpeg when it requests each track URL.
        We fetch the fragment containing that track and redirect to the actual audio URL.
        """
        if not (station_id := request.query.get("station_id")):
            return web.Response(status=400, text="Missing station_id")
        if not (track_num_str := request.query.get("track_num")):
            return web.Response(status=400, text="Missing track_num")

        try:
            track_num = int(track_num_str)
        except ValueError:
            return web.Response(status=400, text="Invalid track_num")

        queue_id = request.query.get("queue_id")
        queue_item_id = request.query.get("queue_item_id")

        # Calculate which fragment and which track within that fragment
        fragment_idx = track_num // TRACKS_PER_FRAGMENT
        track_idx = track_num % TRACKS_PER_FRAGMENT

        self.logger.debug(
            "Stream request: track %d = fragment %d, track %d",
            track_num,
            fragment_idx,
            track_idx,
        )

        try:
            # Fetch the fragment (cached automatically by decorator)
            fragment = await self._get_fragment_data(station_id, fragment_idx)

            # Get the tracks from the fragment
            tracks = fragment.get("tracks", [])
            if track_idx >= len(tracks):
                self.logger.error(
                    "Track index %d out of range (fragment has %d tracks)",
                    track_idx,
                    len(tracks),
                )
                return web.Response(status=404, text="Track not found in fragment")

            track = tracks[track_idx]
            audio_url = track.get("audioURL")

            if not audio_url:
                self.logger.error("No audio URL in track data")
                return web.Response(status=404, text="No audio URL available")

            # Update metadata if we have queue context
            if queue_id and queue_item_id:
                queue_item = self.mass.player_queues.get_item(queue_id, queue_item_id)
                if queue_item and queue_item.streamdetails:
                    # Get the best quality album art
                    album_art_url = None
                    if album_art := track.get("albumArt"):
                        # Get the 500px version (good balance of quality/size)
                        album_art_url = next(
                            (art["url"] for art in album_art if art.get("size") == 500),
                            album_art[-1]["url"] if album_art else None,
                        )
                    queue_item.streamdetails.stream_metadata = StreamMetadata(
                        title=track.get("songTitle", "Unknown Song"),
                        artist=track.get("artistName", "Unknown Artist"),
                        album=track.get("albumTitle"),
                        image_url=album_art_url,
                        duration=track.get("trackLength"),
                    )
            # Redirect to the actual audio URL
            return web.Response(status=302, headers={"Location": audio_url})

        except Exception as err:
            self.logger.error(
                "Error handling stream request for track %d: %s",
                track_num,
                err,
            )
            return web.Response(status=500, text="A stream error occurred")

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 25,
    ) -> SearchResults:
        """Search library radio stations by name."""
        # We can't search Pandora's API without the legacy endpoints,
        # but we can search the user's existing library stations
        if MediaType.RADIO not in media_types:
            return SearchResults()

        results: list[Radio] = []

        async for station in self.get_library_radios():
            if compare_strings(station.name, search_query):
                results.append(station)
                if len(results) >= limit:
                    break

        return SearchResults(radio=results)

    async def browse(self, path: str) -> list[Radio]:
        """Browse radio stations."""
        # For now, just return all stations like get_library_radios
        # Could be enhanced with categories/genres in the future
        return [station async for station in self.get_library_radios()]
