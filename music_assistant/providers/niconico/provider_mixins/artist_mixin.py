"""MixIn for NiconicoMusicProvider: artist-related methods."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Artist, MediaItemType

from music_assistant.providers.niconico.helpers import get_library_items, handle_niconico_errors
from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Track


class NiconicoMusicProviderArtistMixin(NiconicoMusicProviderMixinBase):
    """Artist-related methods for NiconicoMusicProvider."""

    _supported_features = {
        ProviderFeature.ARTIST_TOPTRACKS,
        ProviderFeature.ARTIST_ALBUMS,
        ProviderFeature.LIBRARY_ARTISTS,
        ProviderFeature.LIBRARY_ARTISTS_EDIT,
    }

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        artist = await self.niconico_adapter.user.get_user(prov_artist_id)
        if not artist:
            raise MediaNotFoundError(f"Artist with id {prov_artist_id} not found on Niconico.")
        return artist

    async def get_library_artists(
        self,
    ) -> AsyncGenerator[Artist, None]:
        """Retrieve library artists from the provider."""
        # Get artists from library tracks
        tracks = await get_library_items(
            self.provider,
            cache_key="track",
            query_table="tracks",
            query_method=self.provider.mass.music.tracks.library_items,
        )
        for track in tracks:
            for artist in track.artists:
                if isinstance(artist, Artist):
                    yield artist

        # Include followed artists if user is logged in
        if self.niconico_adapter.auth.is_logged_in():
            async with handle_niconico_errors(self.provider.logger, "fetching following artists"):
                following_artists = await self.niconico_adapter.user.get_own_followings()
                for artist in following_artists:
                    yield artist

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist (user's series)."""
        return await self.niconico_adapter.series.get_user_series(prov_artist_id)

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get newest 50 tracks of an artist."""
        return await self.niconico_adapter.video.get_user_videos(
            prov_artist_id,
            page=1,
            page_size=50,
        )

    async def library_add_for_mixin(self, item: MediaItemType) -> bool | None:
        """Add item to library."""
        if item.media_type == MediaType.ARTIST:
            async with handle_niconico_errors(self.provider.logger, "following artist", item.name):
                success = await self.niconico_adapter.call_with_throttler(
                    self.niconico_adapter.niconico_py_client.user.follow_user,
                    item.item_id,
                )
                if success:
                    self.provider.logger.info("Successfully followed artist %s", item.name)
                    return True
                else:
                    self.provider.logger.warning("Failed to follow artist %s", item.name)

            return False  # API call failed
        return None  # Not handled by this mixin

    async def library_remove_for_mixin(
        self, prov_item_id: str, media_type: MediaType
    ) -> bool | None:
        """Unfollow an artist."""
        if media_type == MediaType.ARTIST:
            async with handle_niconico_errors(
                self.provider.logger, "unfollowing artist", prov_item_id
            ):
                success = await self.niconico_adapter.call_with_throttler(
                    self.niconico_adapter.niconico_py_client.user.unfollow_user,
                    prov_item_id,
                )
                if success:
                    self.provider.logger.info("Successfully unfollowed artist %s", prov_item_id)
                    return True
                else:
                    self.provider.logger.warning("Failed to unfollow artist %s", prov_item_id)

            return False  # API call failed
        return None  # Not handled by this mixin
