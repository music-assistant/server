"""
MixIn for NiconicoMusicProvider: album-related methods.

In this section, we treat NicoNico's "series" as an album.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.constants import CACHE_CATEGORY_MUSIC_PROVIDER_ITEM
from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Track

logger = logging.getLogger(__name__)


class NiconicoMusicProviderAlbumMixin(NiconicoMusicProviderMixinBase):
    """Album-related methods for NiconicoMusicProvider."""

    _supported_features = {
        ProviderFeature.LIBRARY_ALBUMS,
    }

    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id (series as album)."""
        # For Niconico, albums are represented as series
        album_with_tracks = await self.niconico_adapter.series.get_series(prov_album_id)
        if not album_with_tracks:
            raise MediaNotFoundError(f"Album with id {prov_album_id} not found on Niconico.")

        # Update album information for existing tracks in library
        await self._update_tracks_album_info(album_with_tracks.album, album_with_tracks.tracks)

        return album_with_tracks.album

    async def get_library_albums(
        self,
    ) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider (user's own series)."""
        page = 1
        while True:
            albums = await self.niconico_adapter.series.get_own_series_list(
                page=page, page_size=100
            )
            if not albums:
                break

            for album in albums:
                # Update album information for existing tracks in library
                await self._update_tracks_album_info(album)
                yield album

            # If we got fewer albums than page_size, we've reached the end
            if len(albums) < 100:
                break

            page += 1

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id (series tracks)."""
        album_with_tracks = await self.niconico_adapter.series.get_series(prov_album_id)
        if not album_with_tracks:
            return []

        # Update album information for existing tracks in library
        await self._update_tracks_album_info(album_with_tracks.album, album_with_tracks.tracks)
        return album_with_tracks.tracks

    async def _update_tracks_album_info(
        self, album: Album, tracks: list[Track] | None = None
    ) -> None:
        """Update album information for existing tracks in the library."""
        if not tracks:
            tracks = await self.get_album_tracks(album.item_id)

        if not tracks:
            return

        # Update album information in cached tracks
        from music_assistant.helpers.util import TaskManager

        async def cache_track(track: Track) -> None:
            """Cache single track with album information."""
            track.album = album
            cache_key = f"track.{track.item_id}"
            await self.provider.mass.cache.set(
                cache_key,
                track.to_dict(),
                category=CACHE_CATEGORY_MUSIC_PROVIDER_ITEM,
                base_key=self.provider.lookup_key,
            )

        # Process tracks in parallel for better performance
        async with TaskManager(self.provider.mass, limit=10) as task_manager:
            for track in tracks:
                task_manager.create_task(cache_track(track))
