"""Mylist adapter for NicoNico."""

from typing import TYPE_CHECKING

from music_assistant_models.media_items import Playlist

from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.helpers import PlaylistWithTracks
from music_assistant.providers.niconico.parsers import (
    parse_playlist_by_mylist,
    parse_playlist_with_tracks_by_mylist,
)

if TYPE_CHECKING:
    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoMylistAdapter(NiconicoBaseAdapter):
    """Handles mylist related operations for NicoNico."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
        """Initialize NiconicoMylistAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_own_mylists(self) -> list[Playlist]:
        """Get own mylists and parse them."""
        results = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_mylists
        )
        return [parse_playlist_by_mylist(self.adapter.provider, entry) for entry in results]

    async def get_mylist(
        self, mylist_id: str, page_size: int = 500, page: int = 1
    ) -> PlaylistWithTracks | None:
        """Get mylist details and parse as Playlist."""
        mylist = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.get_mylist,
            mylist_id,
            page_size=page_size,
            page=page,
        )
        if not mylist:
            return None
        return parse_playlist_with_tracks_by_mylist(self.adapter.provider, mylist)

    async def get_own_mylist(
        self, mylist_id: str, page_size: int = 500, page: int = 1
    ) -> PlaylistWithTracks | None:
        """Get own mylist details and parse as Playlist."""
        mylist = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_own_mylist,
            mylist_id,
            page_size=page_size,
            page=page,
        )
        if not mylist:
            return None
        return parse_playlist_with_tracks_by_mylist(self.adapter.provider, mylist)
