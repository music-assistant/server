"""
NTS Radio music provider for Music Assistant.

Provides NTS Radio's two live channels and Infinite Mixtapes as
browsable radio stations with live now-playing show metadata.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    ProviderMapping,
    Radio,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
}

NTS_API_LIVE = "https://www.nts.live/api/v2/live"
NTS_API_MIXTAPES = "https://www.nts.live/api/v2/mixtapes"

NTS_LIVE_STREAMS = {
    "1": "https://stream-relay-geo.ntslive.net/stream",
    "2": "https://stream-relay-geo.ntslive.net/stream2",
}

CHANNEL_PREFIX = "nts_channel_"
MIXTAPE_PREFIX = "nts_mixtape_"

METADATA_REFRESH_INTERVAL = 60

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

# NTS source images are landscape; their CDN exposes /resize/ (preserves aspect)
# and /crop/ (center-crop) endpoints. Rewriting picks the square variant so UIs
# that expect square thumbnails don't get a letterboxed result.
IMAGE_CROP_SIZE = 1000
_NTS_IMAGE_OP_RE = re.compile(r"/(?:resize|crop)/\d+x\d+/")


def _square_image_url(url: str | None) -> str | None:
    """Rewrite an NTS image URL to a square center-crop."""
    if not url:
        return None
    return _NTS_IMAGE_OP_RE.sub(f"/crop/{IMAGE_CROP_SIZE}x{IMAGE_CROP_SIZE}/", url, count=1)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return NTSProvider(mass, manifest, config, SUPPORTED_FEATURES)


class NTSProvider(MusicProvider):
    """Provider implementation for NTS Radio."""

    _mixtapes: dict[str, str]
    _unknown_channels: set[str]

    @property
    def max_concurrent_streams(self) -> None:
        """Allow unlimited concurrent upstream source streams."""
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return ()

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self._mixtapes = {}
        self._unknown_channels = set()

        # Live channels use static URLs; metadata enrichment is best-effort.
        try:
            await self._fetch_live_data()
        except ProviderUnavailableError as err:
            self.logger.debug("NTS live metadata unavailable at setup: %s", err)

        # Mixtapes are best-effort too: a transient outage shouldn't take the
        # whole provider offline (live channels still work). Will retry on demand.
        try:
            await self._refresh_mixtape_streams()
        except ProviderUnavailableError as err:
            self.logger.debug("NTS mixtapes unavailable at setup: %s", err)

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """Browse NTS radio stations."""
        path_parts = [] if "://" not in path else path.split("://")[1].split("/")
        subpath = path_parts[0] if path_parts else ""

        if not subpath:
            return [
                BrowseFolder(
                    item_id="live",
                    provider=self.domain,
                    path=path + "live",
                    name="Live Channels",
                    translation_key="live_channels",
                ),
                BrowseFolder(
                    item_id="mixtapes",
                    provider=self.domain,
                    path=path + "mixtapes",
                    name="Infinite Mixtapes",
                    translation_key="infinite_mixtapes",
                ),
            ]

        if subpath == "live":
            return await self._get_live_channels()

        if subpath == "mixtapes":
            return await self._get_mixtapes()

        return []

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id.startswith(CHANNEL_PREFIX):
            channel_name = prov_radio_id.removeprefix(CHANNEL_PREFIX)
            if channel_name in NTS_LIVE_STREAMS:
                try:
                    live_data = await self._fetch_live_data()
                except ProviderUnavailableError:
                    live_data = {}
                api_channel = next(
                    (
                        ch
                        for ch in live_data.get("results", [])
                        if ch.get("channel_name") == channel_name
                    ),
                    None,
                )
                return self._build_channel_radio(channel_name, api_channel)
        elif prov_radio_id.startswith(MIXTAPE_PREFIX):
            alias = prov_radio_id.removeprefix(MIXTAPE_PREFIX)
            try:
                payload = await self._refresh_mixtape_streams()
            except ProviderUnavailableError:
                payload = {}
            if alias in self._mixtapes:
                mixtape = next(
                    (m for m in payload.get("results", []) if m.get("mixtape_alias") == alias),
                    None,
                )
                return self._build_mixtape_radio(alias, mixtape)
        msg = f"NTS radio item {prov_radio_id} not found"
        raise MediaNotFoundError(msg)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for an NTS radio station."""
        stream_url = self._resolve_stream_url(item_id)
        if not stream_url and item_id.startswith(MIXTAPE_PREFIX):
            # mixtape map may be empty if the setup prefetch failed; retry on demand
            try:
                await self._refresh_mixtape_streams()
            except ProviderUnavailableError as err:
                self.logger.debug("NTS mixtape refresh failed: %s", err)
            stream_url = self._resolve_stream_url(item_id)
        if not stream_url:
            msg = f"Could not resolve stream URL for {item_id}"
            raise MediaNotFoundError(msg)

        details = StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=stream_url,
            can_seek=False,
            allow_seek=False,
        )

        if item_id.startswith(CHANNEL_PREFIX):
            details.stream_metadata_update_callback = self._stream_metadata_callback
            details.stream_metadata_update_interval = METADATA_REFRESH_INTERVAL
            # populate initial metadata so the UI doesn't wait an interval
            if (initial := await self._fetch_channel_stream_metadata(item_id)) is not None:
                details.stream_metadata = initial

        return details

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_live_channels(self) -> list[Radio]:
        """Build Radio objects for NTS live channels."""
        try:
            live_data = await self._fetch_live_data()
        except ProviderUnavailableError as err:
            self.logger.debug("NTS live metadata unavailable, returning bare channels: %s", err)
            live_data = {}

        api_channels: dict[str, dict[str, Any]] = {
            ch.get("channel_name", ""): ch for ch in live_data.get("results", [])
        }

        for channel_name in api_channels:
            if (
                channel_name
                and channel_name not in NTS_LIVE_STREAMS
                and channel_name not in self._unknown_channels
            ):
                self.logger.warning(
                    "Unknown NTS channel %r — please report so it can be added",
                    channel_name,
                )
                self._unknown_channels.add(channel_name)

        return [
            self._build_channel_radio(name, api_channels.get(name)) for name in NTS_LIVE_STREAMS
        ]

    def _build_channel_radio(self, channel_name: str, api_channel: dict[str, Any] | None) -> Radio:
        """Build a Radio for a static live channel, enriched with API metadata if available."""
        description_text = ""
        image_url: str | None = None
        if api_channel:
            title, location, description, image_url = self._extract_channel_info(api_channel)
            desc_parts = [f"Now playing: {title}"]
            if location:
                desc_parts.append(f"Broadcasting from {location}")
            if description:
                desc_parts.append(description)
            description_text = "\n".join(desc_parts)
        return self._build_radio(
            item_id=f"{CHANNEL_PREFIX}{channel_name}",
            name=f"NTS {channel_name}",
            description=description_text,
            image_url=image_url,
        )

    @staticmethod
    def _extract_channel_info(channel: dict[str, Any]) -> tuple[str, str, str, str | None]:
        """Extract (title, location, description, image_url) from a live channel payload."""
        channel_name = channel.get("channel_name", "")
        now = channel.get("now", {})
        details = now.get("embeds", {}).get("details", {})
        media = details.get("media", {})
        title = html.unescape(now.get("broadcast_title", f"NTS {channel_name}"))
        location = details.get("location_long", "")
        description = details.get("description", "")
        image_url = _square_image_url(media.get("picture_large") or media.get("background_large"))
        return title, location, description, image_url

    async def _refresh_mixtape_streams(self) -> dict[str, Any]:
        """Fetch the mixtapes payload and refresh the stream URL map. Returns the payload."""
        payload = await self._fetch_mixtapes_data()
        self._mixtapes = {
            alias: endpoint
            for mixtape in payload.get("results", [])
            if (alias := mixtape.get("mixtape_alias"))
            and (endpoint := mixtape.get("audio_stream_endpoint"))
        }
        return payload

    async def _get_mixtapes(self) -> list[Radio]:
        """Build Radio objects for all Infinite Mixtapes."""
        mixtapes_data = await self._refresh_mixtape_streams()
        radios: list[Radio] = []

        for mixtape in mixtapes_data.get("results", []):
            alias = mixtape.get("mixtape_alias", "")
            if not alias or not mixtape.get("audio_stream_endpoint"):
                continue
            radios.append(self._build_mixtape_radio(alias, mixtape))

        return radios

    def _build_mixtape_radio(self, alias: str, mixtape: dict[str, Any] | None) -> Radio:
        """Build a Radio for a mixtape, enriched with API metadata if available."""
        if mixtape:
            title = mixtape.get("title", alias)
            subtitle = mixtape.get("subtitle", "")
            description = mixtape.get("description", "")
            return self._build_radio(
                item_id=f"{MIXTAPE_PREFIX}{alias}",
                name=f"NTS: {title}",
                description=f"{subtitle}\n\n{description}" if subtitle else description,
                image_url=_square_image_url(mixtape.get("media", {}).get("picture_large")),
            )
        return self._build_radio(
            item_id=f"{MIXTAPE_PREFIX}{alias}",
            name=f"NTS: {alias}",
            description="",
            image_url=None,
        )

    @use_cache(3600)
    async def _fetch_mixtapes_data(self) -> dict[str, Any]:
        """Fetch raw Infinite Mixtapes data from the NTS API (cached 1h)."""
        try:
            async with self.mass.http_session.get(NTS_API_MIXTAPES, timeout=HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                data: dict[str, Any] = await resp.json()
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            msg = f"NTS API unavailable: {err}"
            raise ProviderUnavailableError(msg) from err

    def _build_radio(
        self,
        item_id: str,
        name: str,
        description: str,
        image_url: str | None,
    ) -> Radio:
        """Build a Radio object with standard provider mappings."""
        radio = Radio(
            provider=self.instance_id,
            item_id=item_id,
            name=name,
            metadata=MediaItemMetadata(description=description),
            provider_mappings={
                ProviderMapping(
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    item_id=item_id,
                    available=True,
                )
            },
        )
        if image_url:
            radio.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return radio

    def _resolve_stream_url(self, item_id: str) -> str | None:
        """Resolve the stream URL for a given item ID."""
        if item_id.startswith(CHANNEL_PREFIX):
            return NTS_LIVE_STREAMS.get(item_id.removeprefix(CHANNEL_PREFIX))
        if item_id.startswith(MIXTAPE_PREFIX):
            return self._mixtapes.get(item_id.removeprefix(MIXTAPE_PREFIX))
        return None

    async def _stream_metadata_callback(self, stream_details: StreamDetails, _elapsed: int) -> None:
        """Refresh stream metadata during playback (invoked by MA)."""
        if (
            metadata := await self._fetch_channel_stream_metadata(stream_details.item_id)
        ) is not None:
            stream_details.stream_metadata = metadata

    async def _fetch_channel_stream_metadata(self, item_id: str) -> StreamMetadata | None:
        """Fetch live data and build StreamMetadata for the given channel item_id."""
        try:
            live_data = await self._fetch_live_data()
        except ProviderUnavailableError as err:
            self.logger.debug("NTS live data fetch failed: %s", err)
            return None
        for channel in live_data.get("results", []):
            if f"{CHANNEL_PREFIX}{channel.get('channel_name', '')}" == item_id:
                return self._build_stream_metadata(channel)
        return None

    @classmethod
    def _build_stream_metadata(cls, channel: dict[str, Any]) -> StreamMetadata:
        """Build StreamMetadata from a live channel payload."""
        title, location, description, image_url = cls._extract_channel_info(channel)
        desc_parts = []
        if location:
            desc_parts.append(f"Broadcasting from {location}")
        if description:
            desc_parts.append(description)
        return StreamMetadata(
            title=title,
            description="\n".join(desc_parts),
            image_url=image_url,
        )

    @use_cache(METADATA_REFRESH_INTERVAL)
    async def _fetch_live_data(self) -> dict[str, Any]:
        """Fetch current live broadcast data from the NTS API."""
        try:
            async with self.mass.http_session.get(NTS_API_LIVE, timeout=HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                data: dict[str, Any] = await resp.json()
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            msg = f"NTS API unavailable: {err}"
            raise ProviderUnavailableError(msg) from err
