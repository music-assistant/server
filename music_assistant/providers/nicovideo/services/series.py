"""Series adapter for nicovideo."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.helpers import AlbumWithTracks
from music_assistant.providers.nicovideo.services.base import NicovideoBaseService

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album

    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoSeriesService(NicovideoBaseService):
    """Handles series related operations for nicovideo."""

    def __init__(self, adapter: NicovideoServiceManager) -> None:
        """Initialize NicovideoSeriesService with reference to parent adapter."""
        super().__init__(adapter)

    async def get_series_or_own_series(
        self, series_id: str, page: int = 1, page_size: int = 100
    ) -> AlbumWithTracks | None:
        """Get series details with fallback to own series for private series."""
        # Try public series first
        album_with_tracks = await self._get_series(series_id, page=page, page_size=page_size)
        if not album_with_tracks:
            # Fallback to own series (for private series)
            album_with_tracks = await self._get_own_series_detail(
                series_id, page=page, page_size=page_size
            )
        return album_with_tracks

    async def get_user_series(
        self, user_id: str, page: int = 1, page_size: int = 100
    ) -> list[Album]:
        """Get user series and convert as Album list."""
        user_series_items = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_user_series,
            user_id,
            page=page,
            page_size=page_size,
        )
        if not user_series_items:
            return []

        return [
            self.converter_manager.album.convert_by_series(series_item)
            for series_item in user_series_items
        ]

    async def _get_series(
        self, series_id: str, page: int = 1, page_size: int = 100
    ) -> AlbumWithTracks | None:
        """Get series details and convert as AlbumWithTracks."""
        series_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.get_series,
            series_id,
            page=page,
            page_size=page_size,
        )
        if not series_data:
            return None

        return self.converter_manager.album.convert_series_to_album_with_tracks(series_data)

    async def _get_own_series_detail(
        self, series_id: str, page: int = 1, page_size: int = 100
    ) -> AlbumWithTracks | None:
        """Get own series details and convert as AlbumWithTracks."""
        series_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_own_series_detail,
            series_id,
            page=page,
            page_size=page_size,
        )
        if not series_data:
            return None

        return self.converter_manager.album.convert_series_to_album_with_tracks(series_data)
