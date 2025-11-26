"""
MixIn for NicovideoMusicProvider: album-related methods.

In this section, we treat niconico's "series" as an album.
"""

from __future__ import annotations

from typing import override

from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Track  # noqa: TC002 - used in @use_cache

from music_assistant.controllers.cache import use_cache
from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)


class NicovideoMusicProviderAlbumMixin(NicovideoMusicProviderMixinBase):
    """Album-related methods for NicovideoMusicProvider."""

    @override
    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id (series as album)."""
        album_with_tracks = await self.service_manager.series.get_series_or_own_series(
            prov_album_id
        )
        if not album_with_tracks:
            raise MediaNotFoundError(f"Album with id {prov_album_id} not found on nicovideo.")

        return album_with_tracks.album

    @override
    @use_cache(3600 * 24 * 7)  # Cache for 7 days
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id (series tracks)."""
        album_with_tracks = await self.service_manager.series.get_series_or_own_series(
            prov_album_id
        )
        if not album_with_tracks:
            return []

        # Set album information on tracks (cached by @use_cache)
        for track in album_with_tracks.tracks:
            track.album = album_with_tracks.album

        return album_with_tracks.tracks
