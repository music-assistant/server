"""One independently configured remote radio catalog per Music Assistant instance."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout
from music_assistant_models.enums import (
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
CACHE_KEY = "remote_radio_catalog"
SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.LIBRARY_RADIOS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> RemoteRadioCatalogProvider:
    """Create a separate provider object for every native configuration instance."""
    return RemoteRadioCatalogProvider(mass, manifest, config, SUPPORTED_FEATURES)


class RemoteRadioCatalogProvider(MusicProvider):
    """Subscribe to one remote catalog, without shared mutable provider state."""

    async def handle_async_init(self) -> None:
        """Restore only this instance's last good snapshot for the configured URL."""
        self._catalog: dict[str, Station] | None = None
        self._fetch_task: asyncio.Task[None] | None = None
        self._last_error: ResourceTemporarilyUnavailable | None = None
        self._retry_after = 0.0
        try:
            self.catalog_url = validate_url(self.get_setup_value(CONF_CATALOG_URL))
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
        """Cancel this instance's download; MA owns library-sync task cleanup."""
        self.unloading = True
        if self._fetch_task is not None and not self._fetch_task.done():
            self._fetch_task.cancel()
            await asyncio.gather(self._fetch_task, return_exceptions=True)
        if is_removed:
            await self.cache.delete(CACHE_KEY, provider=self.instance_id)
        self._catalog = None

    async def _get_catalog(self, *, for_sync: bool = False) -> dict[str, Station]:
        """Serve a snapshot on outage, but fail sync so no library deletion pass runs."""
        if self.unloading:
            raise ResourceTemporarilyUnavailable("Catalog instance is unloading")
        if for_sync or self._catalog is None:
            await self._refresh()
        return self._catalog if self._catalog is not None else {}

    async def _refresh(self) -> None:
        """Join a shared in-flight request for this instance, never across instances."""
        if self.unloading:
            raise ResourceTemporarilyUnavailable("Catalog instance is unloading")
        if self._last_error is not None and asyncio.get_running_loop().time() < self._retry_after:
            raise self._last_error
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
                text = payload.decode("utf-8-sig", errors="replace")
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
            self._retry_after = 0.0
        except (ClientError, TimeoutError, ValueError) as err:
            # Never expose a signed catalog URL or its query parameters in errors.
            self._last_error = ResourceTemporarilyUnavailable(
                "Unable to retrieve a valid remote radio catalog"
            )
            self._retry_after = asyncio.get_running_loop().time() + 30
            raise self._last_error from err

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
