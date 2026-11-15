"""Hermes Agent Radio music provider for Music Assistant."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import aiohttp
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemMetadata,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    API_RATE_LIMIT,
    API_RATE_PERIOD,
    API_TIMEOUT,
    NOW_PLAYING_URL,
    STATIONS_CACHE_SECONDS,
    STATIONS_URL,
    STREAM_BIT_RATE,
    STREAM_METADATA_UPDATE_INTERVAL,
)
from .helpers import Station, parse_now_playing, parse_stations


class HermesAgentRadio(MusicProvider):
    """Hermes Agent Radio music provider."""

    _throttler: Throttler

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._throttler = Throttler(rate_limit=API_RATE_LIMIT, period=API_RATE_PERIOD)

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        # Without library support the base implementation would report none, which
        # would keep the provider out of radio search and cross-provider matching.
        return {MediaType.RADIO}

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Return the details of a station.

        :param prov_radio_id: The id of the station.
        """
        return self._parse_radio(await self._get_station(prov_radio_id))

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search the lineup of stations by name or tagline.

        :param search_query: The search query.
        :param media_types: A list of media types to include in the search.
        :param limit: The maximum number of search results to return.
        """
        if MediaType.RADIO not in media_types:
            return SearchResults()
        query = search_query.lower()
        stations = await self._get_stations()
        radios = [
            self._parse_radio(station)
            for station in stations.values()
            if query in f"{station.name} {station.tagline}".lower()
        ][:limit]
        return SearchResults(radio=radios)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the stream of a station.

        :param item_id: The id of the station.
        :param media_type: The media type of the item, only radio is offered.
        """
        station = await self._get_station(item_id)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=station.stream_url,
            audio_format=AudioFormat(content_type=ContentType.MP3, bit_rate=STREAM_BIT_RATE),
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
        )

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return all stations.

        :param path: The path to browse, the stations have no sub folders.
        """
        stations = await self._get_stations()
        return [self._parse_radio(station) for station in stations.values()]

    # an expired lineup is still good while the site can't be reached
    @use_cache(STATIONS_CACHE_SECONDS, allow_expired_cache=True)
    async def _fetch_stations(self) -> dict[str, Any]:
        """Return the answer of the station with its list of stations."""
        try:
            async with (
                self._throttler,
                self.mass.http_session.get(STATIONS_URL, timeout=API_TIMEOUT) as response,
            ):
                response.raise_for_status()
                data: dict[str, Any] = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderUnavailableError(f"Hermes Agent Radio is unavailable: {err}") from err
        # raising keeps a bad answer out of the cache, where it would replace a working list
        if not parse_stations(data):
            raise ProviderUnavailableError("Hermes Agent Radio feed returned no stations")
        return data

    async def _get_stations(self) -> dict[str, Station]:
        """Return the stations the lineup holds now, typed."""
        return parse_stations(await self._fetch_stations())

    async def _get_station(self, station_id: str) -> Station:
        """Return one station, raise when the lineup doesn't hold it."""
        station = (await self._get_stations()).get(station_id)
        if station is None:
            raise MediaNotFoundError(f"Radio station {station_id} not found")
        return station

    async def _update_stream_metadata(self, streamdetails: StreamDetails, _elapsed: int) -> None:
        """Show what plays on the station that is playing."""
        try:
            async with (
                self._throttler,
                self.mass.http_session.get(
                    NOW_PLAYING_URL,
                    params={"station": streamdetails.item_id},
                    timeout=API_TIMEOUT,
                ) as response,
            ):
                response.raise_for_status()
                data: dict[str, Any] = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            # says nothing about what plays, so the last known metadata stays
            self.logger.debug("Could not fetch the now-playing feed: %s", err)
            return
        current = parse_now_playing(data)
        if current is None:
            # the feed answered that nothing is on the air; show no title rather than a stale one
            streamdetails.stream_metadata = None
            return
        streamdetails.stream_metadata = StreamMetadata(
            title=current.title,
            artist=current.artist,
            duration=current.duration,
            elapsed_time=current.elapsed_time,
            elapsed_time_last_updated=time.time() if current.elapsed_time is not None else None,
        )

    def _parse_radio(self, station: Station) -> Radio:
        """Return the radio item of a station."""
        lines: list[str] = []
        if station.tagline:
            lines.append(station.tagline)
        if station.songs:
            lines.append(f"{station.songs} community songs in rotation")
        radio = Radio(
            provider=self.instance_id,
            item_id=station.id,
            name=station.name,
            provider_mappings={
                ProviderMapping(
                    item_id=station.id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        radio.metadata = MediaItemMetadata(description="\n".join(lines))
        return radio
