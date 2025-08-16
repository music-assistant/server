"""MixIn for NicovideoMusicProvider: artist-related methods."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, override

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Artist

from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Track


class NicovideoMusicProviderArtistMixin(NicovideoMusicProviderMixinBase):
    """Artist-related methods for NicovideoMusicProvider."""

    _supported_features = {
        ProviderFeature.ARTIST_TOPTRACKS,
        ProviderFeature.ARTIST_ALBUMS,
        ProviderFeature.LIBRARY_ARTISTS,
        ProviderFeature.LIBRARY_ARTISTS_EDIT,
    }

    @override
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
        # Get artists from library tracks (if enabled in config)
        if self.nicovideo_config.get_include_library_track_artists():
            async for track in self.mass.music.tracks.iter_library_items(
                provider=self.instance_id,
            ):
                for artist in track.artists:
                    if isinstance(artist, Artist):
                        yield artist

        # Include followed artists if user is logged in
        if self.service_manager.auth.is_logged_in():
            following_artists = await self.service_manager.user.get_own_followings()
            for artist in following_artists:
                yield artist

    @override
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist (user's series)."""
        return await self.service_manager.series.get_user_series(prov_artist_id)

    @override
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get newest 50 tracks of an artist."""
        return await self.service_manager.video.get_user_videos(
            prov_artist_id,
            page=1,
            page_size=50,
        )
