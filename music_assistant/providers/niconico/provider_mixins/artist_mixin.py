"""MixIn for NiconicoMusicProvider: artist-related methods."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Artist, Track


class NiconicoMusicProviderArtistMixin(NiconicoMusicProviderMixinBase):
    """Artist-related methods for NiconicoMusicProvider."""

    _supported_features = {
        ProviderFeature.ARTIST_TOPTRACKS,
    }

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist = await self.niconico_adapter.user.get_user(prov_artist_id)
        if not artist:
            raise MediaNotFoundError(f"Artist with id {prov_artist_id} not found on Niconico.")
        return artist

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist."""
        # Not implemented yet
        return []

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist."""
        tracks: list[Track] = []
        page: int = 1
        while True:
            page_tracks = await self.niconico_adapter.video.get_user_videos(
                prov_artist_id,
                page=page,
                page_size=50,
            )
            if not page_tracks:
                break
            tracks.extend(page_tracks)
            page += 1
        return tracks
