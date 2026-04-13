"""MixIn for NicovideoMusicProvider: artist-related methods."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import override

from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (  # noqa: TC002 - used in @use_cache
    Album,
    Artist,
    Track,
)

from music_assistant.controllers.cache import use_cache
from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)


class NicovideoMusicProviderArtistMixin(NicovideoMusicProviderMixinBase):
    """Artist-related methods for NicovideoMusicProvider."""

    @override
    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist = await self.service_manager.user.get_user(prov_artist_id)
        if not artist:
            raise MediaNotFoundError(f"Artist with id {prov_artist_id} not found on nicovideo.")
        return artist

    @override
    async def get_library_artists(
        self,
    ) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        # Include followed artists if user is logged in
        following_artists = await self.service_manager.user.get_own_followings()
        for artist in following_artists:
            yield artist

    @override
    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist (user's series)."""
        return await self.service_manager.series.get_user_series(prov_artist_id)

    @override
    @use_cache(3600 * 24 * 14)  # Cache for 14 days
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get newest 50 tracks of an artist."""
        return await self.service_manager.video.get_user_videos(
            prov_artist_id,
            page=1,
            page_size=50,
        )
