"""Mylist adapter for NicoNico."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.converter import (
    convert_playlist_by_mylist,
    convert_playlist_with_tracks_by_mylist,
)
from music_assistant.providers.niconico.helpers import PlaylistWithTracks

if TYPE_CHECKING:
    from music_assistant_models.media_items import Playlist
    from niconico.objects.nvapi import CreateMylistData

    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoMylistAdapter(NiconicoBaseAdapter):
    """Handles mylist related operations for NicoNico."""

    def __init__(self, adapter: NicoNicoMusicAssistantAdapter) -> None:
        """Initialize NiconicoMylistAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_own_mylists(self) -> list[Playlist]:
        """Get own mylists and convert them."""
        results = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_mylists
        )
        if results is None:
            return []
        return [convert_playlist_by_mylist(self.adapter.provider, entry) for entry in results]

    async def get_mylist(
        self, mylist_id: str, page_size: int = 500, page: int = 1
    ) -> PlaylistWithTracks | None:
        """Get mylist details and convert as Playlist."""
        mylist = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.video.get_mylist,
            mylist_id,
            page_size=page_size,
            page=page,
        )
        if not mylist:
            return None
        return convert_playlist_with_tracks_by_mylist(self.adapter.provider, mylist)

    async def get_own_mylist(
        self, mylist_id: str, page_size: int = 500, page: int = 1
    ) -> PlaylistWithTracks | None:
        """Get own mylist details and convert as Playlist."""
        mylist = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_mylist,
            mylist_id,
            page_size=page_size,
            page=page,
        )
        if not mylist:
            return None
        return convert_playlist_with_tracks_by_mylist(self.adapter.provider, mylist)

    async def add_mylist_item(self, mylist_id: str, video_id: str) -> bool:
        """Add a video to mylist."""
        result = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.user.add_mylist_item,
            mylist_id,
            video_id,
        )
        return bool(result)

    async def remove_mylist_items(self, mylist_id: str, video_ids: list[str]) -> bool:
        """Remove videos from mylist."""
        result = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.user.remove_mylist_items,
            mylist_id,
            video_ids,
        )
        return bool(result)

    async def create_mylist(
        self, name: str, description: str = "", is_public: bool = False
    ) -> CreateMylistData | None:
        """Create a new mylist."""
        return await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.user.create_mylist,
            name,
            description=description,
            is_public=is_public,
        )
