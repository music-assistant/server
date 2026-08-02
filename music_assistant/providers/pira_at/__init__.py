"""PIRA.AT Music Provider for Music Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    LinkType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemImage,
    MediaItemLink,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from .catalog import Station, parse_catalog

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


API_URL = "https://pira.at/api/"
WEBSITE_URL = "https://pira.at/"
ICON_URL = "https://pira.at/assets/web-app-manifest-512x512.png"
CATALOG_TTL_SECONDS = 120

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the PIRA.AT provider instance."""
    return PiraAtProvider(mass, manifest, config, SUPPORTED_FEATURES)


class PiraAtProvider(MusicProvider):
    """Expose PIRA.AT's live station catalog as native Music Assistant radio."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the short-lived shared catalog cache."""
        super().__init__(*args, **kwargs)
        self._catalog: dict[str, Station] = {}
        self._catalog_updated = 0.0
        self._catalog_lock = asyncio.Lock()

    @property
    def is_streaming_provider(self) -> bool:
        """Declare this as a remote, changing catalog."""
        return True

    async def handle_async_init(self) -> None:
        """Verify the public catalog before the provider is exposed."""
        await self._async_get_catalog(force=True)

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """Search active stations by name, region, source, or now-playing text."""
        result = SearchResults()
        if MediaType.RADIO not in media_types:
            return result

        query = search_query.casefold().strip()
        if not query:
            return result

        matches = [
            station
            for station in self._async_get_catalog_cached().values()
            if query
            in f"{station.name} {station.region} {station.source} {station.now_playing}".casefold()
        ]
        if not matches:
            matches = [
                station
                for station in (await self._async_get_catalog()).values()
                if query
                in f"{station.name} {station.region} {station.source} {station.now_playing}".casefold()
            ]

        result.radio = [self._as_radio(station) for station in self._sort_stations(matches)[:limit]]
        return result

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """Browse all current stations or stations grouped by region."""
        catalog = await self._async_get_catalog()
        parts = [] if "://" not in path else path.split("://", 1)[1].split("/")
        category = parts[0] if parts and parts[0] else ""

        if not category:
            regions: dict[str, int] = {}
            for station in catalog.values():
                regions[station.region] = regions.get(station.region, 0) + 1

            folders: list[BrowseFolder] = [
                BrowseFolder(
                    item_id="all",
                    provider=self.domain,
                    path=f"{path}all",
                    name="All stations",
                    translation_key="pira_at_all_stations",
                )
            ]
            folders.extend(
                BrowseFolder(
                    item_id=f"region/{quote(region, safe='')}",
                    provider=self.domain,
                    path=f"{path}region/{quote(region, safe='')}",
                    name=f"{region} ({count})",
                )
                for region, count in sorted(
                    regions.items(), key=lambda item: (-item[1], item[0].casefold())
                )
            )
            return folders

        if category == "all":
            return [self._as_radio(station) for station in self._sort_stations(catalog.values())]

        if category == "region" and len(parts) > 1:
            region = unquote(parts[1]).casefold()
            return [
                self._as_radio(station)
                for station in self._sort_stations(catalog.values())
                if station.region.casefold() == region
            ]

        return []

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Return one station using its stable ``source:id`` identifier."""
        station = (await self._async_get_catalog()).get(prov_radio_id)
        if station is None:
            raise MediaNotFoundError(f"PIRA.AT station not found: {prov_radio_id}")
        return self._as_radio(station)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve the current stream URL immediately before playback."""
        if media_type is not MediaType.RADIO:
            raise MediaNotFoundError(f"PIRA.AT item is not radio: {item_id}")
        station = (await self._async_get_catalog()).get(item_id)
        if station is None:
            raise MediaNotFoundError(f"PIRA.AT station not found: {item_id}")

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=station.stream_url,
            can_seek=False,
            allow_seek=False,
        )

    async def _async_get_catalog(self, force: bool = False) -> dict[str, Station]:
        """Fetch once per interval; retain known-good data when a refresh fails."""
        if (
            not force
            and self._catalog
            and monotonic() - self._catalog_updated < CATALOG_TTL_SECONDS
        ):
            return self._catalog

        async with self._catalog_lock:
            if (
                not force
                and self._catalog
                and monotonic() - self._catalog_updated < CATALOG_TTL_SECONDS
            ):
                return self._catalog
            try:
                async with asyncio.timeout(15):
                    async with self.mass.http_session.get(
                        API_URL,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": f"MusicAssistant/{self.mass.version} PIRA.AT provider",
                        },
                    ) as response:
                        response.raise_for_status()
                        catalog = parse_catalog(await response.json(content_type=None))
            except (TimeoutError, aiohttp.ClientError, TypeError, ValueError) as err:
                if self._catalog:
                    self.logger.warning(
                        "PIRA.AT refresh failed; using the last known catalog: %s", err
                    )
                    return self._catalog
                raise ProviderUnavailableError(f"PIRA.AT API unavailable: {err}") from err

            self._catalog = catalog
            self._catalog_updated = monotonic()
            return self._catalog

    def _async_get_catalog_cached(self) -> dict[str, Station]:
        """Return the current catalog without an unnecessary request for a search miss."""
        return self._catalog

    def _as_radio(self, station: Station) -> Radio:
        """Map a PIRA.AT station into a native Music Assistant radio item."""
        radio = Radio(
            item_id=station.item_id,
            provider=self.domain,
            name=station.name,
            provider_mappings={
                ProviderMapping(
                    item_id=station.item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        radio.metadata.popularity = station.listeners
        radio.metadata.links = {MediaItemLink(type=LinkType.WEBSITE, url=WEBSITE_URL)}
        radio.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=ICON_URL,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )
        return radio

    @staticmethod
    def _sort_stations(stations: Iterable[Station]) -> list[Station]:
        """Sort stations like the PIRA.AT site: listeners first, then name."""
        return sorted(
            stations,
            key=lambda station: (-station.listeners, station.name.casefold(), station.item_id),
        )
