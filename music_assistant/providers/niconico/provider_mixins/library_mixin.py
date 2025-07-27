"""MixIn for NiconicoMusicProvider: library edit methods."""

from __future__ import annotations

from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)


class NiconicoMusicProviderLibraryMixin(NiconicoMusicProviderMixinBase):
    """Library edit methods for NiconicoMusicProvider."""

    # _supported_features = {
    #     ProviderFeature.LIBRARY_TRACKS_EDIT,
    #     ProviderFeature.LIBRARY_ALBUMS_EDIT,
    #     ProviderFeature.LIBRARY_ARTISTS_EDIT,
    #     ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    # }

    # async def library_add(self, item: MediaItemType) -> bool:
    #     """Add item to provider's library. Return true on success."""
    #     return True

    # async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
    #     """Remove item from provider's library. Return true on success."""
    #     return True
