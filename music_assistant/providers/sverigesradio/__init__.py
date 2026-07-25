"""Sveriges Radio music provider."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

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

API_BASE: Final = "https://api.sr.se/api/v2"

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
    return SverigesRadio(mass, manifest, config, SUPPORTED_FEATURES)


class SverigesRadio(MusicProvider):
    """Sveriges Radio music provider."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider (none required)."""
        return ()

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """List all Sveriges Radio stations."""
        if (subpath := path.split("://", 1)[1] if "://" in path else "") != "":
            msg = f"Invalid subpath: {subpath}"
            raise KeyError(msg)
        return [self._parse_radio(channel) for channel in await self._get_channels()]

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if channel := await self._get_channel(prov_radio_id):
            return self._parse_radio(channel)
        raise MediaNotFoundError(f"Radio station {prov_radio_id} not found")

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Search Sveriges Radio channels by name."""
        if media_types and MediaType.RADIO not in media_types:
            return SearchResults()
        query = search_query.lower()
        radios = [
            self._parse_radio(channel)
            for channel in await self._get_channels()
            if query in (channel.get("name") or "").lower()
        ][:limit]
        return SearchResults(radio=radios)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for a radio station."""
        # the live audio URL is already part of the cached channel list, so no extra request
        channel = await self._get_channel(item_id)
        url = (channel.get("liveaudio") or {}).get("url") if channel else None
        if not url:
            raise MediaNotFoundError(f"Radio station {item_id} has no live audio URL")
        return StreamDetails(
            provider=self.domain,
            item_id=item_id,
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=url,
            audio_format=AudioFormat(content_type=ContentType.MP3),
            can_seek=False,
            allow_seek=False,
        )

    @use_cache(3600 * 24)  # Cache for 1 day
    async def _get_channels(self) -> list[dict[str, Any]]:
        """Fetch the full list of Sveriges Radio channels."""
        params = {"format": "json", "size": "500"}
        try:
            async with self.mass.http_session.get(
                f"{API_BASE}/channels", params=params, timeout=HTTP_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderUnavailableError("Sveriges Radio API unavailable") from err
        channels: list[dict[str, Any]] = data.get("channels", [])
        return channels

    async def _get_channel(self, channel_id: str) -> dict[str, Any] | None:
        """Return the cached channel payload for an id, or None if unknown."""
        return next(
            (channel for channel in await self._get_channels() if str(channel["id"]) == channel_id),
            None,
        )

    def _parse_radio(self, channel: dict[str, Any]) -> Radio:
        """Build a Radio object from an SR channel payload."""
        channel_id = str(channel["id"])
        radio = Radio(
            name=channel.get("name") or channel.get("channeltype") or f"SR {channel_id}",
            item_id=channel_id,
            provider=self.domain,
            provider_mappings={
                ProviderMapping(
                    item_id=channel_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        if image := channel.get("image"):
            radio.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image,
                    provider=self.domain,
                    remotely_accessible=True,
                )
            )
        return radio
