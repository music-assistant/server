"""Sunshine Live music provider for Music Assistant."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import aiohttp
from music_assistant_models.enums import ImageType, MediaType, StreamType
from music_assistant_models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    UnplayableMediaError,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .constants import API_TIMEOUT, CHANNELS_CACHE_SECONDS, CHANNELS_URL
from .helpers import Channel, parse_channels


class SunshineLiveProvider(MusicProvider):
    """Sunshine Live music provider for Music Assistant."""

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

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Return the details of a channel.

        :param prov_radio_id: The id of the channel.
        """
        return self._parse_radio(prov_radio_id, await self._get_channel(prov_radio_id))

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search the channels by name.

        :param search_query: The text to find in the channel names.
        :param media_types: The media types to search for.
        :param limit: The maximum number of channels to return.
        """
        results = SearchResults()
        query = search_query.lower().strip()
        if MediaType.RADIO not in media_types or not query:
            return results
        channels = await self._get_channels()
        matches = [item for item in channels.items() if query in item[1].name.lower()]
        results.radio = [self._parse_radio(*item) for item in matches[:limit]]
        return results

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the stream of a channel.

        :param item_id: The id of the channel.
        :param media_type: The media type of the item, only radio is offered.
        """
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        channel = await self._get_channel(item_id)
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=channel.content_type,
                bit_rate=channel.bit_rate or 0,
                channels=2,
            ),
            media_type=MediaType.RADIO,
            # the title of the song is in the stream, which the radio stream handling reads
            stream_type=StreamType.HTTP,
            path=channel.stream_url,
            allow_seek=False,
            can_seek=False,
            duration=0,
        )

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return all channels.

        :param path: The path to browse, the channels have no sub folders.
        """
        channels = await self._get_channels()
        return [self._parse_radio(*item) for item in channels.items()]

    async def _get_channel(self, channel_id: str) -> Channel:
        """Return one channel, or raise when the station doesn't offer it."""
        channel = (await self._get_channels()).get(channel_id)
        if channel is None:
            raise MediaNotFoundError(f"Unknown channel: {channel_id}")
        return channel

    async def _get_channels(self) -> dict[str, Channel]:
        """Return the channels the station offers now, or raise when there is no list."""
        channels = parse_channels(await self._fetch_channels())
        if not channels:
            raise ProviderUnavailableError("Sunshine Live offers no channels")
        return channels

    # an expired list is still good while the station can't be reached
    @use_cache(CHANNELS_CACHE_SECONDS, allow_expired_cache=True)
    async def _fetch_channels(self) -> dict[str, Any]:
        """Return the answer of the station with its list of channels."""
        try:
            async with self.mass.http_session.get(CHANNELS_URL, timeout=API_TIMEOUT) as response:
                response.raise_for_status()
                data: dict[str, Any] = await response.json(content_type=None)
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise ProviderUnavailableError(f"Sunshine Live is unavailable: {err}") from err

    def _parse_radio(self, channel_id: str, channel: Channel) -> Radio:
        """Return the radio item of a channel."""
        radio = Radio(
            provider=self.instance_id,
            item_id=channel_id,
            name=channel.name,
            provider_mappings={
                ProviderMapping(
                    item_id=channel_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                )
            },
        )
        radio.metadata.add_image(
            MediaItemImage(
                provider=self.instance_id,
                type=ImageType.THUMB,
                path=channel.image,
                remotely_accessible=True,
            )
        )
        return radio
