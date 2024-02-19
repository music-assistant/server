"""Basic provider allowing for external URL's to be streamed."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from music_assistant.common.models.enums import ContentType, ImageType, MediaType
from music_assistant.common.models.media_items import (
    Artist,
    AudioFormat,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    StreamDetails,
    Track,
)
from music_assistant.server.helpers.audio import (
    get_file_stream,
    get_http_stream,
    get_radio_stream,
    resolve_radio_stream,
)
from music_assistant.server.helpers.playlists import fetch_playlist
from music_assistant.server.helpers.tags import AudioTags, parse_tags
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.common.models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return URLProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return ()  # we do not have any config entries (yet)


class URLProvider(MusicProvider):
    """Music Provider for manual URL's/files added to the queue."""

    def __init__(
        self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
    ) -> None:
        """Initialize MusicProvider."""
        super().__init__(mass, manifest, config)
        self._full_url = {}

    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        return await self.parse_item(prov_track_id)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        return await self.parse_item(prov_radio_id, force_radio=True)

    async def get_artist(self, prov_artist_id: str) -> Track:
        """Get full artist details by id."""
        artist = prov_artist_id
        # this is here for compatibility reasons only
        return Artist(
            item_id=artist,
            provider=self.domain,
            name=artist,
            provider_mappings={
                ProviderMapping(
                    item_id=artist,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=False,
                )
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
        self,
        item_id_or_url: str,
        force_refresh: bool = False,
        force_radio: bool = False,
    ) -> Track | Radio:
        """Parse plain URL to MediaItem of type Radio or Track."""
        item_id, url, media_info = await self._get_media_info(item_id_or_url, force_refresh)
        is_radio = media_info.get("icy-name") or not media_info.duration
        provider_mappings = {
            ProviderMapping(
                item_id=item_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
                audio_format=AudioFormat(
                    content_type=ContentType.try_parse(media_info.format),
                    sample_rate=media_info.sample_rate,
                    bit_depth=media_info.bits_per_sample,
                    bit_rate=media_info.bit_rate,
                ),
            )
        }
        if is_radio or force_radio:
            # treat as radio
            media_item = Radio(
                item_id=item_id,
                provider=self.domain,
                name=media_info.get("icy-name") or media_info.title,
                provider_mappings=provider_mappings,
            )
        else:
            media_item = Track(
                item_id=item_id,
                provider=self.domain,
                name=media_info.title,
                duration=int(media_info.duration or 0),
                artists=[await self.get_artist(artist) for artist in media_info.artists],
                provider_mappings=provider_mappings,
            )

        if media_info.has_cover_image:
            media_item.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=url, provider="file")
            ]
        return media_item

    async def _get_media_info(
        self, item_id_or_url: str, force_refresh: bool = False
    ) -> tuple[str, str, AudioTags]:
        """Retrieve (cached) mediainfo for url."""
        # check if the radio stream is not a playlist
        if item_id_or_url.endswith(("m3u8", "m3u", "pls")):
            playlist = await fetch_playlist(self.mass, item_id_or_url)
            url = playlist[0]
            item_id = item_id_or_url
            self._full_url[item_id] = url
        else:
            url = self._full_url.get(item_id_or_url, item_id_or_url)
            item_id = item_id_or_url
        cache_key = f"{self.instance_id}.media_info.{item_id}"
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
        if is_radio:
            url, supports_icy = await resolve_radio_stream(self.mass, url)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(media_info.format),
                sample_rate=media_info.sample_rate,
                bit_depth=media_info.bits_per_sample,
            ),
            media_type=MediaType.RADIO if is_radio else MediaType.TRACK,
            direct=None if is_radio and supports_icy else url,
            data=url,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        if streamdetails.media_type == MediaType.RADIO:
            # radio stream url
            async for chunk in get_radio_stream(self.mass, streamdetails.data, streamdetails):
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
