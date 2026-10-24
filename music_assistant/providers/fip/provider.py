"""FIP Music Provider for Music Assistant."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

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

from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.music_provider import MusicProvider

from .constants import (
    API_TIMEOUT,
    FIP_STATIONS,
    LIVE_URL,
    OVERDUE_UPDATE_INTERVAL,
    PULL_URL,
    STREAM_METADATA_UPDATE_INTERVAL,
    STREAM_URL,
)
from .helpers import (
    LiveDisplay,
    LiveItem,
    build_stream_metadata,
    parse_live,
    parse_pull,
    parse_radio,
)

# Key in StreamDetails.data under which a playing stream keeps its LiveDisplay.
LIVE_DISPLAY_KEY = "fip_live_display"


class FipProvider(MusicProvider):
    """FIP Music Provider for Music Assistant."""

    _throttler: Throttler

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this provider can serve."""
        # Without library support the base implementation would report none, which would
        # keep the provider out of search-based lookups.
        return {MediaType.RADIO}

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._throttler = Throttler(rate_limit=5, period=1.0)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id not in FIP_STATIONS:
            raise MediaNotFoundError(f"Unknown radio station: {prov_radio_id}")
        return parse_radio(prov_radio_id, self.instance_id, self.domain)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on the FIP stations."""
        results = SearchResults()
        if MediaType.RADIO not in media_types:
            return results
        search_query_lower = search_query.lower().strip()
        if not search_query_lower:
            return results
        radios: list[Radio] = []
        for station_id, station in FIP_STATIONS.items():
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
        if item_id not in FIP_STATIONS:
            raise MediaNotFoundError(f"Unknown radio station: {item_id}")

        stream_details = StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.AAC,
                channels=2,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=STREAM_URL.format(slug=item_id.replace("_", "")),
            allow_seek=False,
            can_seek=False,
            duration=0,
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
            data={LIVE_DISPLAY_KEY: LiveDisplay()},
        )
        # Set initial metadata so the first frame the listener sees
        # is the live song rather than an empty banner.
        await self._update_stream_metadata(stream_details, 0)
        return stream_details

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items: the FIP stations."""
        return [
            parse_radio(station_id, self.instance_id, self.domain) for station_id in FIP_STATIONS
        ]

    async def _live_answer(self, api_id: int) -> tuple[LiveItem | None, LiveItem | None] | None:
        """
        Get what a station plays now and next, or None if the service is unavailable.

        :param api_id: The station's id at the now-playing service.
        """
        data = await self._get_live(api_id)
        if data is None:
            return None
        current, upcoming = parse_live(data)
        # while the service answers with its placeholder, the older one stands in
        if current is None or not current.is_timed:
            pulled = await self._get_pull(api_id)
            if pulled is not None and (song := parse_pull(pulled, time.time()))[0] is not None:
                return song
        return current, upcoming

    async def _get_live(self, api_id: int) -> dict[str, Any] | None:
        """
        Get the now-playing answer for a station, or None if unavailable.

        :param api_id: The station's id at the now-playing service.
        """
        return await self._fetch_json(LIVE_URL.format(api_id=api_id), api_id)

    async def _get_pull(self, api_id: int) -> dict[str, Any] | None:
        """
        Get the older now-playing service's answer for a station, or None if unavailable.

        :param api_id: The station's id at the now-playing service.
        """
        return await self._fetch_json(PULL_URL.format(api_id=api_id), api_id)

    async def _fetch_json(self, url: str, api_id: int) -> dict[str, Any] | None:
        """
        Get a JSON answer of a now-playing service, or None if unavailable.

        :param url: The address to ask.
        :param api_id: The station's id, for the log.
        """
        try:
            async with (
                self._throttler,
                self.mass.http_session.get(url, timeout=API_TIMEOUT) as response,
            ):
                if response.status != 200:
                    self.logger.debug(
                        "FIP now-playing service returned status %s for station %s",
                        response.status,
                        api_id,
                    )
                    return None
                data: dict[str, Any] = await response.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            self.logger.debug("FIP now-playing request failed for station %s: %s", api_id, err)
            return None
        return data

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Update stream metadata callback called by player queue controller.

        :param stream_details: StreamDetails object to update with metadata.
        :param elapsed_time: Elapsed playback time in seconds (unused for FIP).
        """
        display: LiveDisplay = stream_details.data[LIVE_DISPLAY_KEY]
        answer = await self._live_answer(FIP_STATIONS[stream_details.item_id]["api_id"])
        now = time.time()
        # without an answer, a song already announced still takes over when it is due
        current, upcoming = answer if answer is not None else (None, None)
        if display.update(current, upcoming, now):
            stream_details.stream_metadata = build_stream_metadata(display.shown, now)
            self.logger.debug(
                "Updating stream metadata for %s: %s",
                stream_details.item_id,
                display.shown.title if display.shown else None,
            )
        # ask more often while the song on display should already be over
        stream_details.stream_metadata_update_interval = (
            OVERDUE_UPDATE_INTERVAL if display.overdue(now) else STREAM_METADATA_UPDATE_INTERVAL
        )
