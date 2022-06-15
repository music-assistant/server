"""Basic provider allowing for external URL's to be streamed."""
from __future__ import annotations

import os
from typing import AsyncGenerator, List, Optional

from music_assistant.helpers.audio import (
    get_file_stream,
    get_http_stream,
    get_radio_stream,
)
from music_assistant.models.config import MusicProviderConfig
from music_assistant.models.enums import ContentType, MediaType, ProviderType
from music_assistant.models.media_items import MediaItemType, StreamDetails
from music_assistant.models.music_provider import MusicProvider

PROVIDER_CONFIG = MusicProviderConfig(ProviderType.URL)


class URLProvider(MusicProvider):
    """Music Provider for manual URL's/files added to the queue."""

    _attr_name: str = "URL"
    _attr_type: ProviderType = ProviderType.URL
    _attr_available: bool = True
    _attr_supported_mediatypes: List[MediaType] = []

    async def setup(self) -> bool:
        """
        Handle async initialization of the provider.

        Called when provider is registered.
        """
        return True

    async def search(
        self, search_query: str, media_types=Optional[List[MediaType]], limit: int = 5
    ) -> List[MediaItemType]:
        """Perform search on musicprovider."""
        return []

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Get streamdetails for a track/radio."""
        url = item_id
        return StreamDetails(
            provider=ProviderType.URL,
            item_id=item_id,
            content_type=ContentType.try_parse(url),
            media_type=MediaType.URL,
            data=url,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        if streamdetails.media_type == MediaType.RADIO:
            # radio stream url
            async for chunk in get_radio_stream(
                self.mass, streamdetails.data, streamdetails
            ):
                yield chunk
        elif os.path.isfile(streamdetails.data):
            # local file
            async for chunk in get_file_stream(
                self.mass, streamdetails.data, streamdetails, seek_position
            ):
                yield chunk
        else:
            # regular stream url (without icy meta and reconnect)
            async for chunk in get_http_stream(
                self.mass, streamdetails.data, streamdetails, seek_position
            ):
                yield chunk
