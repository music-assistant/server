"""MixIn for NicovideoMusicProvider: track-related methods."""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Track  # noqa: TC002 - used in @use_cache

from music_assistant.controllers.cache import use_cache
from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails


class NicovideoMusicProviderTrackMixin(NicovideoMusicProviderMixinBase):
    """Track-related methods for NicovideoMusicProvider."""

    @override
    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track = await self.service_manager.video.get_video(prov_track_id)
        if not track:
            raise MediaNotFoundError(f"Track with id {prov_track_id} not found on nicovideo.")
        return track

    @override
    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details (streaming URL and format) for given item."""
        if media_type is not MediaType.TRACK:
            raise MediaNotFoundError(f"Media type {media_type} is not supported for stream details")
        # The cached track provides the artist and album for the stream metadata
        track = await self.get_track(item_id)
        return await self.service_manager.video.get_stream_details(item_id, track)
