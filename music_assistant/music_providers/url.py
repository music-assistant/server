"""Basic provider allowing for external URL's to be streamed."""
from __future__ import annotations

import os
from typing import AsyncGenerator, Tuple

from music_assistant.helpers.audio import (
    get_file_stream,
    get_http_stream,
    get_radio_stream,
)
from music_assistant.helpers.playlists import fetch_playlist
from music_assistant.helpers.tags import AudioTags, parse_tags
from music_assistant.models.config import MusicProviderConfig
from music_assistant.models.enums import (
    ContentType,
    ImageType,
    MediaQuality,
    MediaType,
    ProviderType,
)
from music_assistant.models.media_items import (
    Artist,
    MediaItemImage,
    MediaItemProviderId,
    MediaItemType,
    Radio,
    StreamDetails,
    Track,
)
from music_assistant.models.music_provider import MusicProvider

PROVIDER_CONFIG = MusicProviderConfig(ProviderType.URL)

# pylint: disable=arguments-renamed


class URLProvider(MusicProvider):
    """Music Provider for manual URL's/files added to the queue."""

    _attr_name: str = "URL"
    _attr_type: ProviderType = ProviderType.URL
    _attr_available: bool = True
    _full_url = {}

    async def setup(self) -> bool:
        """
        Handle async initialization of the provider.

        Called when provider is registered.
        """
        return True

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return await self.parse_item(prov_track_id)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        return await self.parse_item(prov_radio_id)

    async def get_artist(self, prov_artist_id: str) -> Track:
        """Get full artist details by id."""
        artist = prov_artist_id
        # this is here for compatibility reasons only
        return Artist(
            artist,
            self.type,
            artist,
            provider_ids={
                MediaItemProviderId(artist, self.type, self.id, available=False)
            },
        )

    async def get_item(self, media_type: MediaType, prov_item_id: str) -> MediaItemType:
        """Get single MediaItem from provider."""
        if media_type == MediaType.ARTIST:
            return await self.get_artist(prov_item_id)
        if media_type == MediaType.TRACK:
            return await self.get_track(prov_item_id)
        if media_type == MediaType.RADIO:
            return await self.get_radio(prov_item_id)
        if media_type == MediaType.UNKNOWN:
            return await self.parse_item(prov_item_id)
        raise NotImplementedError

    async def parse_item(
        self, item_id_or_url: str, force_refresh: bool = False
    ) -> Track | Radio:
        """Parse plain URL to MediaItem of type Radio or Track."""
        item_id, url, media_info = await self._get_media_info(
            item_id_or_url, force_refresh
        )
        is_radio = media_info.get("icy-name") or not media_info.duration
        if is_radio:
            # treat as radio
            media_item = Radio(
                item_id=item_id,
                provider=self.type,
                name=media_info.get("icy-name") or media_info.title,
            )
        else:
            media_item = Track(
                item_id=item_id,
                provider=self.type,
                name=media_info.title,
                duration=int(media_info.duration or 0),
                artists=[
                    await self.get_artist(artist) for artist in media_info.artists
                ],
            )

        quality = MediaQuality.from_file_type(media_info.format)
        media_item.provider_ids = {
            MediaItemProviderId(item_id, self.type, self.id, quality=quality)
        }
        if media_info.has_cover_image:
            media_item.metadata.images = [MediaItemImage(ImageType.THUMB, url, True)]
        return media_item

    async def _get_media_info(
        self, item_id_or_url: str, force_refresh: bool = False
    ) -> Tuple[str, str, AudioTags]:
        """Retrieve (cached) mediainfo for url."""
        # check if the radio stream is not a playlist
        if (
            item_id_or_url.endswith("m3u8")
            or item_id_or_url.endswith("m3u")
            or item_id_or_url.endswith("pls")
        ):
            playlist = await fetch_playlist(self.mass, item_id_or_url)
            url = playlist[0]
            item_id = item_id_or_url
            self._full_url[item_id] = url
        elif "?" in item_id_or_url or "&" in item_id_or_url:
            # store the 'real' full url to be picked up later
            # this makes sure that we're not storing any temporary data like auth keys etc
            # a request for an url mediaitem always passes here first before streamdetails
            url = item_id_or_url
            item_id = item_id_or_url.split("?")[0].split("&")[0]
            self._full_url[item_id] = url
        else:
            url = self._full_url.get(item_id_or_url, item_id_or_url)
            item_id = item_id_or_url
        cache_key = f"{self.type.value}.media_info.{item_id}"
        # do we have some cached info for this url ?
        cached_info = await self.mass.cache.get(cache_key)
        if cached_info and not force_refresh:
            media_info = AudioTags.parse(cached_info)
        else:
            # parse info with ffprobe (and store in cache)
            media_info = await parse_tags(url)
            if "authSig" in url:
                media_info.has_cover_image = False
            await self.mass.cache.set(cache_key, media_info.raw)
        return (item_id, url, media_info)

    async def get_stream_details(self, item_id: str) -> StreamDetails | None:
        """Get streamdetails for a track/radio."""
        item_id, url, media_info = await self._get_media_info(item_id)
        is_radio = media_info.get("icy-name") or not media_info.duration
        return StreamDetails(
            provider=self.type,
            item_id=item_id,
            content_type=ContentType.try_parse(media_info.format),
            media_type=MediaType.RADIO if is_radio else MediaType.TRACK,
            sample_rate=media_info.sample_rate,
            bit_depth=media_info.bits_per_sample,
            direct=None if is_radio else url,
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
            # regular stream url (without icy meta)
            async for chunk in get_http_stream(
                self.mass, streamdetails.data, streamdetails, seek_position
            ):
                yield chunk
