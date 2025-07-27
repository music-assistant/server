"""MixIn for NiconicoMusicProvider: album-related methods."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Track


class NiconicoMusicProviderAlbumMixin(NiconicoMusicProviderMixinBase):
    """Album-related methods for NiconicoMusicProvider."""

    # _supported_features = {
    #      ProviderFeature.LIBRARY_ALBUMS,
    # }

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        # Not implemented yet
        raise NotImplementedError("get_album is not implemented.")

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        # Not implemented yet
        return []
