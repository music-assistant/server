"""ABC Radio Music Provider for Music Assistant."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from .constants import (
    ABC_RADIO_STATIONS,
    API_TIMEOUT,
    NOW_PLAYING_URL,
    STREAM_METADATA_UPDATE_INTERVAL,
)
from .helpers import parse_now_playing, parse_radio

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamMetadata


class ABCRadioProvider(MusicProvider):
    """ABC Radio Music Provider for Music Assistant."""

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id not in ABC_RADIO_STATIONS:
            raise MediaNotFoundError(f"Unknown radio station: {prov_radio_id}")
        return parse_radio(prov_radio_id, self.instance_id, self.domain)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on the ABC Radio stations."""
        results = SearchResults()
        if MediaType.RADIO not in media_types:
            return results
        search_query_lower = search_query.lower().strip()
        if not search_query_lower:
            return results
        radios: list[Radio] = []
        for station_id, station in ABC_RADIO_STATIONS.items():
            if search_query_lower in station["name"].lower():
                radios.append(parse_radio(station_id, self.instance_id, self.domain))
                if len(radios) >= limit:
                    break
        results.radio = radios
        return results

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        if item_id not in ABC_RADIO_STATIONS:
            raise MediaNotFoundError(f"Unknown radio station: {item_id}")

        station = ABC_RADIO_STATIONS[item_id]
        stream_details = StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.AAC,
                channels=2,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HLS,
            path=station["stream_url"],
            allow_seek=False,
            can_seek=False,
            duration=0,
        )
        if station["has_track_data"]:
            stream_details.stream_metadata_update_callback = self._update_stream_metadata
            stream_details.stream_metadata_update_interval = STREAM_METADATA_UPDATE_INTERVAL
            # Set initial metadata so the first frame the listener sees
            # is the live track rather than an empty banner.
            stream_details.stream_metadata = await self._get_now_playing(item_id)
        return stream_details

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        return [
            parse_radio(station_id, self.instance_id, self.domain)
            for station_id in ABC_RADIO_STATIONS
        ]

    async def _get_now_playing(self, station_id: str) -> StreamMetadata | None:
        """
        Get the currently playing track for a station, or None if unavailable.

        :param station_id: A known ABC Radio station id.
        """
        url = NOW_PLAYING_URL.format(station_id=station_id)
        try:
            async with self.mass.http_session.get(url, timeout=API_TIMEOUT) as response:
                if response.status != 200:
                    self.logger.debug(
                        "ABC Radio API returned status %s for station %s",
                        response.status,
                        station_id,
                    )
                    return None
                data: dict[str, Any] = await response.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            self.logger.debug("ABC Radio API request failed for station %s: %s", station_id, err)
            return None
        return parse_now_playing(data)

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Update stream metadata callback called by player queue controller.

        :param stream_details: StreamDetails object to update with metadata.
        :param elapsed_time: Elapsed playback time in seconds (unused for ABC Radio).
        """
        metadata = await self._get_now_playing(stream_details.item_id)
        if metadata:
            self.logger.debug(
                "Updating stream metadata for %s: %s - %s",
                stream_details.item_id,
                metadata.artist,
                metadata.title,
            )
            stream_details.stream_metadata = metadata
        elif stream_details.stream_metadata:
            # No track playing (e.g. talk programming): clear stale track info.
            stream_details.stream_metadata = None
