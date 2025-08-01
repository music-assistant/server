"""MixIn for NiconicoMusicProvider: library methods."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderFeature

from music_assistant.providers.niconico.helpers import handle_niconico_errors
from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import MediaItemType


class NiconicoMusicProviderLibraryMixin(NiconicoMusicProviderMixinBase):
    """Library edit methods for NiconicoMusicProvider."""

    _supported_features = {
        ProviderFeature.LIBRARY_TRACKS_EDIT,
    }

    async def library_add_for_mixin(self, item: MediaItemType) -> bool | None:
        """Add item to provider's library. Return true on success."""
        # Only support tracks for now
        if item.media_type != MediaType.TRACK:
            return None  # Not handled by this mixin

        # Check if auto-like is enabled
        auto_like_enabled = self.niconico_config.get_auto_like_on_library_add()
        if not auto_like_enabled:
            return True  # Successfully "added" but no action needed

        async with handle_niconico_errors(self.provider.logger, "liking video", item.item_id):
            # Extract video ID from provider item ID
            video_id = item.item_id

            # Like the video using niconico.py
            like_result = await self.niconico_adapter.call_with_throttler(
                self.niconico_adapter.niconico_py_client.video.like_video, video_id
            )

            if like_result:
                self.provider.logger.info("Successfully liked video %s", video_id)
            else:
                self.provider.logger.warning("Failed to like video %s", video_id)

        # Always return True for library add, regardless of like success/failure
        return True

    async def library_remove_for_mixin(
        self, prov_item_id: str, media_type: MediaType
    ) -> bool | None:
        """Remove item from provider's library. Return true on success."""
        # Only support tracks for now
        if media_type != MediaType.TRACK:
            return None  # Not handled by this mixin

        # For now, we don't implement unlike functionality for tracks
        # because Niconico's "like" feature is more of an optional engagement feature
        # rather than a core library management feature.
        return True
