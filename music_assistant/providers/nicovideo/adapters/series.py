"""Series adapter for nicovideo."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.adapters.base import NicovideoBaseAdapter
from music_assistant.providers.nicovideo.converter import (
    convert_album_by_series,
    convert_series_to_album_with_tracks,
)
from music_assistant.providers.nicovideo.helpers import AlbumWithTracks

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album

    from music_assistant.providers.nicovideo.adapter import NicovideoMusicAssistantAdapter


class NicovideoSeriesAdapter(NicovideoBaseAdapter):
    """Handles series related operations for nicovideo."""

    def __init__(self, adapter: NicovideoMusicAssistantAdapter) -> None:
        """Initialize NicovideoSeriesAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_series(
        self, series_id: str, page: int = 1, page_size: int = 100
    ) -> AlbumWithTracks | None:
        """Get series details and convert as AlbumWithTracks."""
        series_data = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.video.get_series,
            series_id,
            page=page,
            page_size=page_size,
        )
        if not series_data:
            return None

        return convert_series_to_album_with_tracks(self.adapter.provider, series_data)

    async def get_user_series(
        self, user_id: str, page: int = 1, page_size: int = 100
    ) -> list[Album]:
        """Get user series and convert as Album list."""
        user_series_items = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.user.get_user_series,
            user_id,
            page=page,
            page_size=page_size,
        )
        if not user_series_items:
            return []

        return [
            convert_album_by_series(self.adapter.provider, series_item)
            for series_item in user_series_items
        ]

    async def get_own_series_list(self, page: int = 1, page_size: int = 100) -> list[Album]:
        """Get own series list and convert as Album list."""
        if not self.adapter.auth.is_logged_in():
            return []

        user_series_items = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_series_list,
            page=page,
            page_size=page_size,
        )
        if not user_series_items:
            return []

        return [
            convert_album_by_series(self.adapter.provider, series_item)
            for series_item in user_series_items
        ]

    async def get_own_series(
        self, series_id: str, page: int = 1, page_size: int = 100
    ) -> AlbumWithTracks | None:
        """Get own series details and convert as AlbumWithTracks."""
        if not self.adapter.auth.is_logged_in():
            return None

        series_data = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_series,
            series_id,
            page=page,
            page_size=page_size,
        )
        if not series_data:
            return None

        return convert_series_to_album_with_tracks(self.adapter.provider, series_data)
