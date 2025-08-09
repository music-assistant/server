"""MixIn for NicovideoMusicProvider: artist-related methods."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, override

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Artist, MediaItemType

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
        artist = await self.service_hub.user.get_user(prov_artist_id)
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
        if self.service_hub.auth.is_logged_in():
            following_artists = await self.service_hub.user.get_own_followings()
            for artist in following_artists:
                yield artist

    @override
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of all albums for the given artist (user's series)."""
        return await self.service_hub.series.get_user_series(prov_artist_id)

    @override
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get newest 50 tracks of an artist."""
        return await self.service_hub.video.get_user_videos(
            prov_artist_id,
            page=1,
            page_size=50,
        )

    async def library_add_for_mixin(self, item: MediaItemType) -> bool | None:
        """Add item to library."""
        if item.media_type == MediaType.ARTIST:
            # Check if follow/unfollow artists is enabled
            auto_sync_enabled = self.nicovideo_config.get_use_follow_unfollow_artists()
            if not auto_sync_enabled:
                return True  # Successfully "added" but no action needed

            success = await self.service_hub.user.follow_user(item.item_id)
            if success:
                self.logger.debug("Successfully followed artist %s", item.name)
                return True
            else:
                self.logger.warning("Failed to follow artist %s", item.name)
                # Raise error with user-friendly message
                raise ProviderUnavailableError(
                    f"Failed to follow artist '{item.name}' on niconico video. "
                    f"This might be due to API limits or network issues."
                )

        return None  # Not handled by this mixin

    async def library_remove_for_mixin(
        self, prov_item_id: str, media_type: MediaType
    ) -> bool | None:
        """Remove artist from library."""
        if media_type == MediaType.ARTIST:
            # Check if follow/unfollow artists is enabled
            auto_sync_enabled = self.nicovideo_config.get_use_follow_unfollow_artists()
            if not auto_sync_enabled:
                return True  # Successfully "removed" but no action needed

            success = await self.service_hub.user.unfollow_user(prov_item_id)
            if success:
                self.logger.debug("Successfully unfollowed artist %s", prov_item_id)
                return True
            else:
                self.logger.warning("Failed to unfollow artist %s", prov_item_id)
                # Raise error with user-friendly message
                raise ProviderUnavailableError(
                    f"Failed to unfollow artist (ID: {prov_item_id}) on niconico video. "
                    f"This might be due to API limits or network issues."
                )

        return None  # Not handled by this mixin
