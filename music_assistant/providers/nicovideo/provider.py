"""
NicovideoMusicProvider: Coordinator that combines all mixins.

This is the main provider class that acts as a coordinator and aggregator:
- Combines all domain-specific mixins (Track, Playlist, Album, Artist, etc.)
- Delegates cross-mixin operations through _for_mixin patterns
- Handles provider-wide operations that span multiple domains

Architecture Overview:
├── services/: API integration and data transformation coordination
│   └── Coordinates API calls through niconico.py, manages rate limiting, and delegates conversion
├── converters/: Data transformation layer
│   └── Converts niconico objects to Music Assistant models
└── provider_mixins/: Business logic layer
    └── Implements Music Assistant provider interface methods
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.nicovideo.mixin_caller import MixinCaller

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.nicovideo.provider_mixins import NICOVIDEO_MIXINS


class NicovideoMusicProvider(
    *NICOVIDEO_MIXINS,  # type: ignore[misc]
):
    """Coordinator combining all nicovideo provider mixins."""

    @override
    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        await MixinCaller(self).invoke_all(
            lambda mixin_class: mixin_class.handle_async_init_for_mixin
        )

    @override
    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        await MixinCaller(self, is_reverse=True).invoke_all(
            lambda mixin_class: mixin_class.unload_for_mixin, is_removed
        )

    @override
    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details (streaming URL and format) for given item."""
        return await MixinCaller(self).invoke_first_valid_or_raise(
            MediaNotFoundError("Stream unknown"),
            lambda mixin_class: mixin_class.get_stream_details_for_mixin,
            item_id,
            media_type,
        )

    @override
    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        return await MixinCaller(self).invoke_first_valid(
            False, lambda mixin_class: mixin_class.library_add_for_mixin, item
        )

    @override
    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        return await MixinCaller(self).invoke_first_valid(
            False,
            lambda mixin_class: mixin_class.library_remove_for_mixin,
            prov_item_id,
            media_type,
        )
