"""One independently configured remote radio catalog per Music Assistant instance."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    AudioFormat,
    MediaItemImage,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from .catalog import MAX_CATALOG_BYTES, Station, parse_catalog, validate_url

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

CONF_CATALOG_URL = "catalog_url"
CONF_REFRESH_INTERVAL = "refresh_interval"
DEFAULT_REFRESH_MINUTES = 30
CACHE_KEY = "remote_radio_catalog"
SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> RemoteRadioCatalogProvider:
    """Create a separate provider object for every native configuration instance."""
    return RemoteRadioCatalogProvider(mass, manifest, config, SUPPORTED_FEATURES)


class RemoteRadioCatalogProvider(MusicProvider):
    """Subscribe to one remote catalog, without shared mutable provider state."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Expose the minute interval; URLs are edited through native reconfiguration."""
        return (
            ConfigEntry(
                key=CONF_REFRESH_INTERVAL,
                type=ConfigEntryType.INTEGER,
                required=True,
                default_value=self.get_setup_value(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_MINUTES),
                range=(1, 1440),
            ),
        )

    async def handle_async_init(self) -> None:
        """Restore only this instance's last good snapshot for the configured URL."""
        self._catalog: dict[str, Station] | None = None
        self._fetch_task: asyncio.Task[None] | None = None
        self._last_error: ResourceTemporarilyUnavailable | None = None
        self._next_refresh = 0.0
        self._timer_id = f"remote_radio_catalog.refresh.{self.instance_id}"
        try:
            self.catalog_url = validate_url(self.get_setup_value(CONF_CATALOG_URL))
            interval = self.config.get_value(
                CONF_REFRESH_INTERVAL,
                self.get_setup_value(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_MINUTES),
            )
            if (
                not isinstance(interval, int)
                or isinstance(interval, bool)
                or not 1 <= interval <= 1440
            ):
                raise ValueError("Refresh interval must be between 1 and 1440 minutes")  # noqa: TRY301
            self.refresh_seconds = interval * 60
        except ValueError as err:
            raise InvalidDataError(str(err)) from err
        self._checksum = hashlib.sha256(self.catalog_url.encode()).hexdigest()
        cached = await self.cache.get(
            CACHE_KEY, provider=self.instance_id, checksum=self._checksum, allow_expired_cache=True
        )
        if isinstance(cached, dict):
            try:
                self._catalog = parse_catalog(cached["text"], cached["base_url"])
            except KeyError, TypeError, ValueError:
                self.logger.warning("Ignoring invalid cached radio catalog")

    async def loaded_in_mass(self) -> None:
        """Start this instance's native timer; initial library sync remains MA-owned."""
        self._schedule_refresh(delay=0)

    @property
    def is_streaming_provider(self) -> bool:
        """Each instance has unique contents, unlike accounts for a shared service."""
        return False

    @property
    def instance_name_postfix(self) -> str | None:
        """Supply a safe default; the native provider name field can override it."""
        return self.instance_id.rsplit("--", 1)[-1]

    async def get_library_radios(self) -> AsyncGenerator[Radio]:
        """Use native library sync, and never interpret a failed fetch as deletions."""
        for station in (await self._get_catalog(for_sync=True)).values():
            yield self._radio(station)

    async def browse(self, path: str) -> list[Radio]:
        """Show the catalog beneath its independently named provider source."""
        return [self._radio(station) for station in (await self._get_catalog()).values()]

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search only the catalog belonging to this instance."""
        result = SearchResults()
        if MediaType.RADIO in media_types:
            result.radio = [
                self._radio(station)
                for station in (await self._get_catalog()).values()
                if search_query.casefold() in station.name.casefold()
            ][:limit]
        return result

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Resolve a station ID within this instance, including an offline snapshot."""
        if station := (await self._get_catalog()).get(prov_radio_id):
            return self._radio(station)
        raise MediaNotFoundError("Station is no longer in this catalog")

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Let MA's existing stream and player pipeline handle playback."""
        station = (await self._get_catalog()).get(item_id)
        if media_type != MediaType.RADIO or station is None:
            raise MediaNotFoundError("Station is not in this catalog")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=station.url,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            can_seek=False,
            allow_seek=False,
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Cancel and await only our timer callback/download; MA stops library sync."""
        self.unloading = True
        self.mass.cancel_timer(self._timer_id)
        pending = {
            task
            for task in (self.mass.get_task(self._timer_id), self._fetch_task)
            if task is not None and not task.done()
        }
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # A timer already inside start_sync could have queued work during core teardown.
        # After joining that callback, make a final native cancellation barrier.
        await self.mass.music.unschedule_provider_sync(
            self.instance_id, clear_persisted_state=is_removed
        )
        if is_removed:
            await self.cache.delete(CACHE_KEY, provider=self.instance_id)
        self._catalog = None

    def _schedule_refresh(self, delay: float | None = None) -> None:
        """Use MA's per-instance timer API (recurring task schedules are hourly)."""
        if not self.unloading:
            self.mass.call_later(
                self.refresh_seconds if delay is None else delay,
                self._scheduled_refresh,
                task_id=self._timer_id,
            )

    async def _scheduled_refresh(self) -> None:
        """Refresh one source, then queue only its native library synchronization."""
        try:
            await self._get_catalog(for_sync=True)
            if not self.unloading:
                await self.mass.music.start_sync(
                    media_types=[MediaType.RADIO], providers=[self.instance_id]
                )
        except ResourceTemporarilyUnavailable:
            self.logger.warning("Catalog refresh failed; retaining the last good catalog")
        finally:
            self._schedule_refresh()

    async def _get_catalog(self, *, for_sync: bool = False) -> dict[str, Station]:
        """Serve a snapshot on outage, but fail sync so no library deletion pass runs."""
        if self.unloading:
            raise ResourceTemporarilyUnavailable("Catalog instance is unloading")
        try:
            if asyncio.get_running_loop().time() >= self._next_refresh:
                await self._refresh()
            if self._last_error is not None:
                raise self._last_error
        except ResourceTemporarilyUnavailable:
            if for_sync or self._catalog is None:
                raise
        return self._catalog if self._catalog is not None else {}

    async def _refresh(self) -> None:
        """Join a shared in-flight request for this instance, never across instances."""
        if self.unloading:
            raise ResourceTemporarilyUnavailable("Catalog instance is unloading")
        if self._fetch_task is None or self._fetch_task.done():
            self._fetch_task = self.mass.create_task(
                self._download(),
                task_id=f"remote_radio_catalog.fetch.{self.instance_id}",
                task_name="Fetch remote radio catalog",
                log_exceptions=False,
            )
        # A cancelled browser request must not cancel another waiter's library sync.
        await asyncio.shield(self._fetch_task)

    async def _download(self) -> None:
        """Bound HTTP work and commit a fully validated snapshot atomically."""
        try:
            async with self.mass.http_session.get(
                self.catalog_url, timeout=ClientTimeout(total=30)
            ) as response:
                response.raise_for_status()
                payload = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    payload.extend(chunk)
                    if len(payload) > MAX_CATALOG_BYTES:
                        raise ValueError("Radio catalog exceeds 2 MiB")  # noqa: TRY301
                text = payload.decode("utf-8-sig")
                base_url = validate_url(str(response.url))
                catalog = parse_catalog(text, base_url)
            await self.cache.set(
                CACHE_KEY,
                {"text": text, "base_url": base_url},
                provider=self.instance_id,
                checksum=self._checksum,
                persistent=True,
                allow_expired_cache=True,
            )
            self._catalog = catalog
            self._last_error = None
        except (ClientError, TimeoutError, ValueError) as err:
            # Never expose a signed catalog URL or its query parameters in errors.
            self._last_error = ResourceTemporarilyUnavailable(
                "Unable to retrieve a valid remote radio catalog"
            )
            self._next_refresh = asyncio.get_running_loop().time() + self.refresh_seconds
            raise self._last_error from err
        # A cancelled download is not a successful (possibly empty) snapshot.
        self._next_refresh = asyncio.get_running_loop().time() + self.refresh_seconds

    def _radio(self, station: Station) -> Radio:
        """Use native instance namespaces for media IDs, mappings and source labels."""
        radio = Radio(
            item_id=station.item_id,
            provider=self.instance_id,
            name=station.name,
            provider_mappings={
                ProviderMapping(
                    item_id=station.item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    is_unique=True,
                    details=self.name,
                    audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                )
            },
        )
        radio.metadata.description = f"{self.name} — {urlsplit(self.catalog_url).hostname}"
        if station.logo:
            radio.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=station.logo,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return radio
