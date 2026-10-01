"""Global Player music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import ContentType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    API_TIMEOUT,
    BRANDS_URL,
    CACHE_TTL_PLAYABLE,
    CACHE_TTL_STATIONS,
    HEADERS,
    PLAYABLE_URL,
)
from .helpers import parse_radio, parse_stream_url

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return GlobalPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)


class GlobalPlayerProvider(MusicProvider):
    """Provider implementation for Global Player UK."""

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        return {MediaType.RADIO}

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (CONF_ENTRY_UNOFFICIAL_PROVIDER,)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Get full radio details by id.

        :param prov_radio_id: The station identifier.
        """
        stations = await self._get_stations()
        if prov_radio_id not in stations:
            raise MediaNotFoundError(f"Unknown station: {prov_radio_id}")
        return parse_radio(stations[prov_radio_id], self.instance_id, self.domain)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Browse this provider's radio stations.

        :param path: The browse path.
        """
        stations = await self._get_stations()
        return [
            parse_radio(station_data, self.instance_id, self.domain)
            for station_data in stations.values()
        ]

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Perform search on Global Player channels.

        :param search_query: The search term to match against station titles or descriptions.
        :param media_types: Media types to search.
        :param limit: Maximum number of search results to return.
        """
        results = SearchResults()
        if MediaType.RADIO not in media_types:
            return results
        query = search_query.lower().strip()
        if not query:
            return results
        stations = await self._get_stations()
        matches: list[Radio] = []
        for station_data in stations.values():
            name = station_data.get("name", "").lower()
            tagline = station_data.get("tagline", "").lower()
            brand = station_data.get("brandName", "").lower()
            if query in name or query in tagline or query in brand:
                matches.append(parse_radio(station_data, self.instance_id, self.domain))
                if len(matches) >= limit:
                    break
        results.radio = matches
        return results

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get stream details for a radio station.

        :param item_id: The station identifier.
        :param media_type: The media type of the requested item.
        """
        playable_data = await self._get_playable(item_id)
        stream_url = parse_stream_url(playable_data, item_id)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=stream_url,
        )

    @use_cache(CACHE_TTL_STATIONS)
    async def _get_stations(self) -> dict[str, dict[str, Any]]:
        """Fetch and return all stations indexed by station id."""
        data = await self._get_json(BRANDS_URL)
        return {str(station["id"]): station for station in data}

    @use_cache(CACHE_TTL_PLAYABLE)
    async def _get_playable(self, station_id: str) -> dict[str, Any]:
        """Fetch playable stream metadata for a station."""
        url = PLAYABLE_URL.format(playable_id=station_id)
        data: dict[str, Any] = await self._get_json(url, station_id)
        return data

    async def _get_json(self, url: str, station_id: str | None = None) -> Any:
        """Fetch JSON data from the Global Player API."""
        try:
            async with self.mass.http_session.get(
                url, headers=HEADERS, timeout=API_TIMEOUT
            ) as response:
                if response.status == 404 and station_id:
                    raise MediaNotFoundError(f"Global Player station not found: {station_id}")
                response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderUnavailableError("Global Player API unavailable") from err
