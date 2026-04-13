"""Mylist adapter for nicovideo."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.helpers import PlaylistWithTracks
from music_assistant.providers.nicovideo.services.base import NicovideoBaseService

if TYPE_CHECKING:
    from music_assistant_models.media_items import Playlist
    from niconico.objects.nvapi import CreateMylistData

    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoMylistService(NicovideoBaseService):
    """Handles mylist related operations for nicovideo."""

    def __init__(self, adapter: NicovideoServiceManager) -> None:
        """Initialize NicovideoMylistService with reference to parent adapter."""
        super().__init__(adapter)

    async def get_own_mylists(self) -> list[Playlist]:
        """Get own mylists and convert them."""
        results = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_own_mylists
        )
        if results is None:
            return []
        return [self.converter_manager.playlist.convert_by_mylist(entry) for entry in results]

    async def get_mylist_or_own_mylist(
        self, mylist_id: str, page_size: int = 500, page: int = 1
    ) -> PlaylistWithTracks | None:
        """Get mylist with fallback to own_mylist for private mylists."""
        # Try public mylist first
        playlist_with_tracks = await self._get_mylist(mylist_id, page_size=page_size, page=page)
        if not playlist_with_tracks:
            # Fallback to own mylist (for private mylists)
            playlist_with_tracks = await self.get_own_mylist(
                mylist_id, page_size=page_size, page=page
            )
        return playlist_with_tracks

    async def get_own_mylist(
        self, mylist_id: str, page_size: int = 500, page: int = 1
    ) -> PlaylistWithTracks | None:
        """Get own mylist details and convert as Playlist."""
        mylist = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_own_mylist,
            mylist_id,
            page_size=page_size,
            page=page,
        )
        if not mylist:
            return None
        playlist_with_tracks = self.converter_manager.playlist.convert_with_tracks_by_mylist(mylist)
        self._update_positions_in_playlist(playlist_with_tracks)
        return playlist_with_tracks

    async def add_mylist_item(self, mylist_id: str, video_id: str) -> bool:
        """Add a video to mylist."""
        result = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.add_mylist_item,
            mylist_id,
            video_id,
        )
        return bool(result)

    async def remove_mylist_items(self, mylist_id: str, video_ids: list[str]) -> bool:
        """Remove videos from mylist."""
        result = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.remove_mylist_items,
            mylist_id,
            video_ids,
        )
        return bool(result)

    async def create_mylist(
        self, name: str, description: str = "", is_public: bool = False
    ) -> CreateMylistData | None:
        """Create a new mylist."""
        return await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.create_mylist,
            name,
            description=description,
            is_public=is_public,
        )

    async def _get_mylist(
        self, mylist_id: str, page_size: int = 500, page: int = 1
    ) -> PlaylistWithTracks | None:
        """Get mylist details and convert as Playlist."""
        mylist = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.get_mylist,
            mylist_id,
            page_size=page_size,
            page=page,
        )
        if not mylist:
            return None
        playlist_with_tracks = self.converter_manager.playlist.convert_with_tracks_by_mylist(mylist)
        self._update_positions_in_playlist(playlist_with_tracks)
        return playlist_with_tracks

    def _update_positions_in_playlist(self, playlist: PlaylistWithTracks) -> None:
        """Update positions in playlist tracks."""
        # Ensure tracks have position set (1-based)
        for index, track in enumerate(playlist.tracks, start=1):
            track.position = index
