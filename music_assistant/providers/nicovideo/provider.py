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

from typing import override

from music_assistant.providers.nicovideo.provider_mixins import (
    NicovideoMusicProviderAlbumMixin,
    NicovideoMusicProviderArtistMixin,
    NicovideoMusicProviderCoreMixin,
    NicovideoMusicProviderExplorerMixin,
    NicovideoMusicProviderPlaylistMixin,
    NicovideoMusicProviderTrackMixin,
)

# Tuple of mixin classes in inheritance order.
# Used for provider-wide operations that span all mixins (e.g. init, unload)
NICOVIDEO_MIXINS = (
    NicovideoMusicProviderCoreMixin,
    NicovideoMusicProviderTrackMixin,
    NicovideoMusicProviderPlaylistMixin,
    NicovideoMusicProviderArtistMixin,
    NicovideoMusicProviderAlbumMixin,
    NicovideoMusicProviderExplorerMixin,
)


class NicovideoMusicProvider(
    NicovideoMusicProviderCoreMixin,
    NicovideoMusicProviderTrackMixin,
    NicovideoMusicProviderPlaylistMixin,
    NicovideoMusicProviderArtistMixin,
    NicovideoMusicProviderAlbumMixin,
    NicovideoMusicProviderExplorerMixin,
):
    """Coordinator combining all nicovideo provider mixins."""

    @override
    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        for mixin_class in NICOVIDEO_MIXINS:
            await mixin_class.handle_async_init_for_mixin(self)

    @override
    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for mixin_class in NICOVIDEO_MIXINS[::-1]:
            await mixin_class.unload_for_mixin(self, is_removed)
