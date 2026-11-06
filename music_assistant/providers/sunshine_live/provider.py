"""Sunshine Live music provider for Music Assistant."""

from __future__ import annotations

from collections.abc import Sequence

from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError
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

from music_assistant.models.music_provider import MusicProvider

from .constants import CHANNELS, STREAM_BIT_RATE, STREAM_SAMPLE_RATE, STREAM_URL


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
        if prov_radio_id not in CHANNELS:
            raise MediaNotFoundError(f"Unknown channel: {prov_radio_id}")
        return self._parse_radio(prov_radio_id)

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
        matches = [key for key, channel in CHANNELS.items() if query in channel["name"].lower()]
        results.radio = [self._parse_radio(key) for key in matches[:limit]]
        return results

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the stream of a channel.

        :param item_id: The id of the channel.
        :param media_type: The media type of the item, only radio is offered.
        """
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        if item_id not in CHANNELS:
            raise MediaNotFoundError(f"Unknown channel: {item_id}")
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.MP3,
                sample_rate=STREAM_SAMPLE_RATE,
                bit_rate=STREAM_BIT_RATE,
                channels=2,
            ),
            media_type=MediaType.RADIO,
            # the title of the song is in the stream, which the radio stream handling reads
            stream_type=StreamType.HTTP,
            path=STREAM_URL.format(path=CHANNELS[item_id]["path"]),
            allow_seek=False,
            can_seek=False,
            duration=0,
        )

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return all channels.

        :param path: The path to browse, the channels have no sub folders.
        """
        return [self._parse_radio(key) for key in CHANNELS]

    def _parse_radio(self, channel_id: str) -> Radio:
        """Return the radio item of a known channel."""
        channel = CHANNELS[channel_id]
        radio = Radio(
            provider=self.instance_id,
            item_id=channel_id,
            name=channel["name"],
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
                path=channel["image"],
                remotely_accessible=True,
            )
        )
        return radio
