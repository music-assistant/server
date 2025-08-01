"""Series adapter for NicoNico."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.helpers import AlbumWithTracks
from music_assistant.providers.niconico.parsers import (
    parse_album_by_series,
    parse_series_to_album_with_tracks,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album

    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoSeriesAdapter(NiconicoBaseAdapter):
    """Handles series related operations for NicoNico."""

    def __init__(self, adapter: NicoNicoMusicAssistantAdapter) -> None:
        """Initialize NiconicoSeriesAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_series(
        self, series_id: str, page: int = 1, page_size: int = 100
    ) -> AlbumWithTracks | None:
        """Get series details and parse as AlbumWithTracks."""
        series_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.get_series,
            series_id,
            page=page,
            page_size=page_size,
        )
        if not series_data:
            return None

        return parse_series_to_album_with_tracks(self.adapter.provider, series_data)

    async def get_user_series(
        self, user_id: str, page: int = 1, page_size: int = 100
    ) -> list[Album]:
        """Get user series and parse as Album list."""
        user_series_items = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_user_series,
            user_id,
            page=page,
            page_size=page_size,
        )
        if not user_series_items:
            return []

        return [
            parse_album_by_series(self.adapter.provider, series_item)
            for series_item in user_series_items
        ]

    async def get_own_series_list(self, page: int = 1, page_size: int = 100) -> list[Album]:
        """Get own series list and parse as Album list."""
        if not self.adapter.auth.is_logged_in():
            return []

        user_series_items = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_series_list,
            page=page,
            page_size=page_size,
        )
        if not user_series_items:
            return []

        return [
            parse_album_by_series(self.adapter.provider, series_item)
            for series_item in user_series_items
        ]

    async def get_own_series(
        self, series_id: str, page: int = 1, page_size: int = 100
    ) -> AlbumWithTracks | None:
        """Get own series details and parse as AlbumWithTracks."""
        if not self.adapter.auth.is_logged_in():
            return None

        series_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_series,
            series_id,
            page=page,
            page_size=page_size,
        )
        if not series_data:
            return None

        return parse_series_to_album_with_tracks(self.adapter.provider, series_data)
