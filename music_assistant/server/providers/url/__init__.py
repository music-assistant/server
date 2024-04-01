"""Basic provider allowing for external URL's to be streamed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.common.models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant.common.models.errors import MediaNotFoundError
from music_assistant.common.models.media_items import (
    Artist,
    AudioFormat,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    Track,
)
from music_assistant.common.models.streamdetails import StreamDetails
from music_assistant.server.helpers.tags import AudioTags, parse_tags
from music_assistant.server.models.music_provider import MusicProvider

if TYPE_CHECKING:
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
        # always prefer db item for existing items to not overwrite user customizations
        db_item = await self.mass.music.tracks.get_library_item_by_prov_id(
            prov_track_id, self.instance_id
        )
        if db_item is None and not prov_track_id.startswith("http"):
            msg = f"Track not found: {prov_track_id}"
            raise MediaNotFoundError(msg)
        return await self.parse_item(prov_track_id)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        # always prefer db item for existing items to not overwrite user customizations
        db_item = await self.mass.music.radio.get_library_item_by_prov_id(
            prov_radio_id, self.instance_id
        )
        if db_item is None and not prov_radio_id.startswith("http"):
            msg = f"Radio not found: {prov_radio_id}"
            raise MediaNotFoundError(msg)
        return await self.parse_item(prov_radio_id)

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
        url: str,
        force_refresh: bool = False,
        force_radio: bool = False,
    ) -> Track | Radio:
        """Parse plain URL to MediaItem of type Radio or Track."""
        media_info = await self._get_media_info(url, force_refresh)
        is_radio = media_info.get("icy-name") or not media_info.duration
        provider_mappings = {
            ProviderMapping(
                item_id=url,
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
                item_id=url,
                provider=self.domain,
                name=media_info.get("icy-name") or url,
                provider_mappings=provider_mappings,
            )
        else:
            media_item = Track(
                item_id=url,
                provider=self.domain,
                name=media_info.title or url,
                duration=int(media_info.duration or 0),
                artists=[await self.get_artist(artist) for artist in media_info.artists],
                provider_mappings=provider_mappings,
            )

        if media_info.has_cover_image:
            media_item.metadata.images = [
                MediaItemImage(type=ImageType.THUMB, path=url, provider="file")
            ]
        return media_item

    async def _get_media_info(self, url: str, force_refresh: bool = False) -> AudioTags:
        """Retrieve mediainfo for url."""
        # do we have some cached info for this url ?
        cache_key = f"{self.instance_id}.media_info.{url}"
        cached_info = await self.mass.cache.get(cache_key)
        if cached_info and not force_refresh:
            return AudioTags.parse(cached_info)
        # parse info with ffprobe (and store in cache)
        media_info = await parse_tags(url)
        if "authSig" in url:
            media_info.has_cover_image = False
        await self.mass.cache.set(cache_key, media_info.raw)
        return media_info

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get streamdetails for a track/radio."""
        media_info = await self._get_media_info(item_id)
        is_radio = media_info.get("icy-name") or not media_info.duration
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(media_info.format),
                sample_rate=media_info.sample_rate,
                bit_depth=media_info.bits_per_sample,
            ),
            media_type=MediaType.RADIO if is_radio else MediaType.TRACK,
            stream_type=StreamType.HTTP,
            path=item_id,
            can_seek=not is_radio,
        )
