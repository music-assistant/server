"""nicovideo music provider module for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.errors import MediaNotFoundError

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType, ProviderFeature
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.nicovideo.provider_mixins import (
    NicovideoMusicProviderAlbumMixin,
    NicovideoMusicProviderArtistMixin,
    NicovideoMusicProviderCoreMixin,
    NicovideoMusicProviderExplorerMixin,
    NicovideoMusicProviderPlaylistMixin,
    NicovideoMusicProviderTrackMixin,
)

NICOVIDEO_MIXINS = (
    NicovideoMusicProviderCoreMixin,
    NicovideoMusicProviderTrackMixin,
    NicovideoMusicProviderPlaylistMixin,
    NicovideoMusicProviderArtistMixin,
    NicovideoMusicProviderAlbumMixin,
    NicovideoMusicProviderExplorerMixin,
)


class NicovideoMusicProvider(
    *NICOVIDEO_MIXINS,  # type: ignore[misc]
    MusicProvider,
):
    """nicovideo music provider for Music Assistant."""

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        all_features: set[ProviderFeature] = set()

        # Collect features from defined Mixins
        for mixin_class in NICOVIDEO_MIXINS:
            all_features.update(mixin_class.get_supported_features_for_mixin())

        return all_features

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details (streaming URL and format) for given item."""
        for mixin_class in NICOVIDEO_MIXINS:
            details = await mixin_class.get_stream_details_for_mixin(self, item_id, media_type)
            if details:
                return details
        raise MediaNotFoundError("Stream unknown")

    async def library_add(self, item: MediaItemType) -> bool:
        """Add item to provider's library. Return true on success."""
        for mixin_class in NICOVIDEO_MIXINS:
            result = await mixin_class.library_add_for_mixin(self, item)
            if result is not None:
                return result
        # If no mixin handled it, return False (not supported)
        return False

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove item from provider's library. Return true on success."""
        for mixin_class in NICOVIDEO_MIXINS:
            result = await mixin_class.library_remove_for_mixin(self, prov_item_id, media_type)
            if result is not None:
                return result
        # If no mixin handled it, return False (not supported)
        return False

    @property
    def provider(self) -> MusicProvider:
        """NicovideoMusicProviderProtocol implementation."""
        return self
