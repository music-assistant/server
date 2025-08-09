"""
MixIn for NicovideoMusicProvider: album-related methods.

In this section, we treat niconico's "series" as an album.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, override

from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.helpers.util import TaskManager
from music_assistant.providers.nicovideo.helpers import cache_track
from music_assistant.providers.nicovideo.provider_mixins.mixin_base import (
    NicovideoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Track

logger = logging.getLogger(__name__)


class NicovideoMusicProviderAlbumMixin(NicovideoMusicProviderMixinBase):
    """Album-related methods for NicovideoMusicProvider."""

    _supported_features = {
        ProviderFeature.LIBRARY_ALBUMS,
    }

    @override
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id (series as album)."""
        album_with_tracks = await self.adapter_hub.series.get_series(prov_album_id)
        if not album_with_tracks:
            raise MediaNotFoundError(f"Album with id {prov_album_id} not found on nicovideo.")

        # Update album information for existing tracks in library
        await self._update_tracks_album_info(album_with_tracks.album, album_with_tracks.tracks)

        return album_with_tracks.album

    @override
    async def get_library_albums(
        self,
    ) -> AsyncGenerator[Album, None]:
        """Retrieve library albums from the provider (user's own series)."""
        if not self.adapter_hub.auth.is_logged_in():
            return

        # Check config setting for including own series as albums
        if not self.nicovideo_config.get_include_own_series_albums():
            return

        page = 1
        while True:
            albums = await self.adapter_hub.series.get_own_series_list(page=page, page_size=100)
            if not albums:
                break

            for album in albums:
                # Update album information for existing tracks in library
                await self._update_tracks_album_info(album, None)
                yield album

            # If we got fewer albums than page_size, we've reached the end
            if len(albums) < 100:
                break

            page += 1

    @override
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id (series tracks)."""
        album_with_tracks = await self.adapter_hub.series.get_series(prov_album_id)
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
            # Get tracks directly from adapter to avoid infinite recursion
            album_with_tracks = await self.adapter_hub.series.get_series(album.item_id)
            if not album_with_tracks:
                return
            tracks = album_with_tracks.tracks

        if not tracks:
            return

        # Update album information in cached tracks
        async def update_track_with_album(track: Track) -> None:
            """Update single track with album information and cache it."""
            track.album = self.adapter_hub.converter_hub.item_mapper.get_album_mapping(
                album.item_id, album.name
            )
            await cache_track(self, track)

        # Process tracks in parallel for better performance with limited concurrency
        async with TaskManager(self.mass, limit=5) as task_manager:
            for track in tracks:
                task_manager.create_task(update_track_with_album(track))
