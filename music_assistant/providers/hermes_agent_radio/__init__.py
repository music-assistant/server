"""Hermes Agent Radio music provider."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemMetadata,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

BASE_URL: Final = "https://hermesagentradio.com"
STATIONS_ENDPOINT: Final = f"{BASE_URL}/stations.json"
NOW_PLAYING_ENDPOINT: Final = f"{BASE_URL}/now-playing.json"

# the now-playing feed for the playing mount is re-read this often
STREAM_METADATA_UPDATE_INTERVAL: Final = 60

# bound each request; the shared session's default timeout is too long for a metadata fetch
HTTP_TIMEOUT: Final = aiohttp.ClientTimeout(total=10)

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize instance."""
    return HermesAgentRadio(mass, manifest, config, SUPPORTED_FEATURES)


class HermesAgentRadio(MusicProvider):
    """
    Hermes Agent Radio music provider.

    The station is a self-running 24/7 broadcast: each 'station' is a linear
    MP3 stream (the main feed plus genre mounts). There is no on-demand track
    audio, so this provider serves MediaType.RADIO only and exposes the
    community song library / chart as browse metadata, not playable tracks.
    """

    @property
    def is_streaming_provider(self) -> bool:
        """Return True: live linear broadcast, audio must never be cached locally."""
        return True

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider (none required)."""
        return ()

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """List all Hermes Agent Radio stations."""
        if (subpath := path.split("://", 1)[1] if "://" in path else "") != "":
            msg = f"Invalid subpath: {subpath}"
            raise KeyError(msg)
        return [self._parse_radio(station) for station in await self._get_stations()]

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        station = await self._get_station(prov_radio_id)
        if not station:
            raise MediaNotFoundError(f"Radio station {prov_radio_id} not found")
        return self._parse_radio(station)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Search Hermes Agent Radio stations by name or tagline."""
        if media_types and MediaType.RADIO not in media_types:
            return SearchResults()
        query = search_query.lower()
        radios = [
            self._parse_radio(station)
            for station in await self._get_stations()
            if query in f"{station.get('name', '')} {station.get('tagline', '')}".lower()
        ][:limit]
        return SearchResults(radio=radios)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for a station."""
        station = await self._get_station(item_id)
        if not station:
            raise MediaNotFoundError(f"Radio station {item_id} not found")
        # the feed gives relative mount paths like /stream and /stream/techno
        path = station.get("stream") or "/stream"
        url = f"{BASE_URL}{path}" if path.startswith("/") else str(path)
        streamdetails = StreamDetails(
            provider=self.domain,
            item_id=item_id,
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=url,
            audio_format=AudioFormat(content_type=ContentType.MP3, bit_rate=320),
            can_seek=False,
            allow_seek=False,
            # radio is live linear audio: never treated as a fixed-duration item
            is_realtime=True,
        )
        # the station's now-playing feed answers per mount (?station=<id>), so every
        # station gets live metadata while it plays, showing its own on-air item
        streamdetails.stream_metadata_update_callback = self._update_stream_metadata
        streamdetails.stream_metadata_update_interval = STREAM_METADATA_UPDATE_INTERVAL
        return streamdetails

    @use_cache(3600)  # station list may gain genre mounts; refresh hourly
    async def _get_stations(self) -> list[dict[str, Any]]:
        """Fetch the station list from the station's open JSON feed."""
        try:
            async with self.mass.http_session.get(STATIONS_ENDPOINT, timeout=HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                data: dict[str, Any] = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderUnavailableError("Hermes Agent Radio feed unavailable") from err
        stations: list[dict[str, Any]] = data.get("stations", [])
        if not stations:
            raise ProviderUnavailableError("Hermes Agent Radio feed returned no stations")
        return stations

    async def _get_station(self, station_id: str) -> dict[str, Any] | None:
        """Return the cached station payload for an id, or None if unknown."""
        return next(
            (station for station in await self._get_stations() if station.get("id") == station_id),
            None,
        )

    async def _update_stream_metadata(self, streamdetails: StreamDetails, _elapsed: int) -> None:
        """Refresh the playing station's live metadata from its mount's now-playing feed."""
        try:
            async with self.mass.http_session.get(
                NOW_PLAYING_ENDPOINT,
                params={"station": streamdetails.item_id},
                timeout=HTTP_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data: dict[str, Any] = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            # says nothing about what plays, so the last known metadata stays
            self.logger.debug("Could not fetch the now-playing feed: %s", err)
            return
        current: dict[str, Any] = data.get("now") or {}
        title = str(current.get("title") or "").strip()
        if not title:
            return
        description: str | None = None
        if upcoming := (data.get("up_next") or [None])[0]:
            if next_title := upcoming.get("title"):
                description = f"Up next: {next_title}"
        duration_s = current.get("duration_s")
        streamdetails.stream_metadata = StreamMetadata(
            title=title,
            artist=str(current["artist"]) if current.get("artist") else None,
            duration=int(duration_s) if duration_s else None,
            description=description,
        )

    def _parse_radio(self, station: dict[str, Any]) -> Radio:
        """Build a Radio object from a station payload (no network I/O)."""
        station_id: str = station.get("id") or "main"
        name: str = station.get("name") or "Hermes Agent Radio"
        lines: list[str] = []
        if tagline := station.get("tagline"):
            lines.append(str(tagline))
        if songs := station.get("songs"):
            lines.append(f"{songs} community songs in rotation")
        radio = Radio(
            name=name,
            item_id=station_id,
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=station_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        radio.metadata = MediaItemMetadata(description="\n".join(lines))
        return radio
