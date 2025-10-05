"""MixIn for NicovideoMusicProvider: track-related methods."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, override

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType, Track
    from music_assistant_models.streamdetails import StreamDetails


class NicovideoMusicProviderTrackMixin(NicovideoMusicProviderMixinBase):
    """Track-related methods for NicovideoMusicProvider."""

    @override
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        track = await self.service_manager.video.get_video(prov_track_id)
        if not track:
            raise MediaNotFoundError(f"Track with id {prov_track_id} not found on nicovideo.")
        return track

    @override
    async def get_library_tracks(
        self,
    ) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from the provider."""
        # Default behavior: include own mylists but not following or own videos
        include_following_tracks = False
        include_own_tracks = True
        include_own_videos_tracks = False

        # Process all library playlists for this provider
        async for playlist in self.mass.music.playlists.iter_library_items(
            provider=self.instance_id,
        ):
            # Filter based on playlist type and config setting
            # Own mylists are editable (is_editable=True)
            # Following mylists are not editable (is_editable=False)
            if playlist.is_editable and not include_own_tracks:
                continue
            if not playlist.is_editable and not include_following_tracks:
                continue

            prov_map = next(iter(playlist.provider_mappings), None)
            if not prov_map:
                continue
            page = 0
            while True:
                playlist_tracks = await self.get_playlist_tracks(prov_map.item_id, page)
                if not playlist_tracks:
                    break
                for track in playlist_tracks:
                    yield track
                page += 1

        # Include own uploaded videos if enabled
        if include_own_videos_tracks:
            own_videos = await self.service_manager.user.get_own_videos()
            for track in own_videos:
                yield track

    @override
    async def get_stream_details_for_mixin(
        self, item_id: str, media_type: MediaType
    ) -> StreamDetails | None:
        """Get stream details (streaming URL and format) for given item."""
        if media_type is not MediaType.TRACK:
            return None

        return await self.service_manager.video.get_stream_details(item_id)

    @override
    async def library_add_for_mixin(self, item: MediaItemType) -> bool | None:
        """Add item to provider's library. Return true on success."""
        if item.media_type is not MediaType.TRACK:
            return None

        # Default: auto-like is enabled
        auto_like_enabled = True
        if not auto_like_enabled:
            return True  # Successfully "added" but no action needed

        # Extract video ID from provider item ID
        video_id = item.item_id

        # Like the video using niconico.py
        like_result = await self.service_manager.video.like_video(video_id)

        if like_result:
            self.logger.debug("Successfully liked video %s", video_id)
        else:
            self.logger.warning("Failed to like video %s", video_id)

        # Always return True for library add, regardless of like success/failure
        return True

    @override
    async def library_remove_for_mixin(
        self, prov_item_id: str, media_type: MediaType
    ) -> bool | None:
        """Remove item from provider's library. Return true on success."""
        if media_type is not MediaType.TRACK:
            return None

        # For now, we don't implement unlike functionality for tracks
        # because niconico's "like" feature is more of an optional engagement feature
        # rather than a core library management feature.
        return True
