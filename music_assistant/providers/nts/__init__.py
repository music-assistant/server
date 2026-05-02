"""
NTS Radio music provider for Music Assistant.

Provides NTS Radio's two live channels and Infinite Mixtapes as
browsable radio stations with live now-playing show metadata.
"""

from __future__ import annotations

import html
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
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
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


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return NTSProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return ()


class NTSProvider(MusicProvider):
    """Provider implementation for NTS Radio."""

    _mixtapes: dict[str, str]
    _unknown_channels: set[str]

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
        # whole provider offline (live channels still work). Will retry on browse.
        try:
            await self._get_mixtapes()
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
                ),
                BrowseFolder(
                    item_id="mixtapes",
                    provider=self.domain,
                    path=path + "mixtapes",
                    name="Infinite Mixtapes",
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
            for radio in await self._get_live_channels():
                if radio.item_id == prov_radio_id:
                    return radio
        elif prov_radio_id.startswith(MIXTAPE_PREFIX):
            for radio in await self._get_mixtapes():
                if radio.item_id == prov_radio_id:
                    return radio
        msg = f"NTS radio item {prov_radio_id} not found"
        raise MediaNotFoundError(msg)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for an NTS radio station."""
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
            await self._stream_metadata_callback(details, 0)

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

        radios: list[Radio] = []
        for channel_name in NTS_LIVE_STREAMS:
            channel = api_channels.get(channel_name)
            description_text = ""
            image_url: str | None = None
            if channel:
                title, location, description, image_url = self._extract_channel_info(channel)
                desc_parts = [f"Now playing: {title}"]
                if location:
                    desc_parts.append(f"Broadcasting from {location}")
                if description:
                    desc_parts.append(description)
                description_text = "\n".join(desc_parts)

            radios.append(
                self._build_radio(
                    item_id=f"{CHANNEL_PREFIX}{channel_name}",
                    name=f"NTS {channel_name}",
                    description=description_text,
                    image_url=image_url,
                )
            )

        return radios

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
        image_url = media.get("picture_large") or media.get("background_large")
        return title, location, description, image_url

    async def _get_mixtapes(self) -> list[Radio]:
        """Build Radio objects for all Infinite Mixtapes."""
        mixtapes_data = await self._fetch_mixtapes_data()
        radios: list[Radio] = []
        mixtape_streams: dict[str, str] = {}

        for mixtape in mixtapes_data.get("results", []):
            alias = mixtape.get("mixtape_alias", "")
            stream_endpoint = mixtape.get("audio_stream_endpoint", "")
            if not alias or not stream_endpoint:
                continue
            title = mixtape.get("title", alias)
            subtitle = mixtape.get("subtitle", "")
            description = mixtape.get("description", "")

            mixtape_streams[alias] = stream_endpoint

            radios.append(
                self._build_radio(
                    item_id=f"{MIXTAPE_PREFIX}{alias}",
                    name=f"NTS: {title}",
                    description=f"{subtitle}\n\n{description}" if subtitle else description,
                    image_url=mixtape.get("media", {}).get("picture_large"),
                )
            )

        self._mixtapes = mixtape_streams
        return radios

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
        try:
            live_data = await self._fetch_live_data()
        except ProviderUnavailableError as err:
            self.logger.debug("NTS live data fetch failed during playback: %s", err)
            return

        for channel in live_data.get("results", []):
            channel_name = channel.get("channel_name", "")
            if f"{CHANNEL_PREFIX}{channel_name}" != stream_details.item_id:
                continue

            title, location, description, image_url = self._extract_channel_info(channel)
            desc_parts = []
            if location:
                desc_parts.append(f"Broadcasting from {location}")
            if description:
                desc_parts.append(description)

            stream_details.stream_metadata = StreamMetadata(
                title=title,
                description="\n".join(desc_parts),
                image_url=image_url,
            )
            break

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
