"""Search adapter for nicovideo."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Playlist, Track
from niconico.objects.video.search import (
    EssentialMylist,
    EssentialSeries,
)

from music_assistant.providers.nicovideo.services.base import NicovideoBaseService

if TYPE_CHECKING:
    from music_assistant_models.media_items import SearchResults

    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoSearchService(NicovideoBaseService):
    """Handles search related operations for nicovideo."""

    def __init__(self, adapter: NicovideoServiceManager) -> None:
        """Initialize NicovideoSearchService with reference to parent adapter."""
        super().__init__(adapter)

    async def search_playlists_and_albums_by_keyword(
        self,
        search_query: str,
        limit: int,
        search_result: SearchResults,
        media_types: list[MediaType],
    ) -> None:
        """Search for playlists (mylists) and albums (series) by keyword."""
        if not media_types:
            return

        search_playlists = MediaType.PLAYLIST in media_types
        search_albums = MediaType.ALBUM in media_types

        playlists_to_add = []
        albums_to_add = []

        # Search for mylists and series separately to work around API bug
        # where specifying both types returns only series
        if search_playlists:
            mylists = await self._search_mylists_by_keyword(search_query, limit)
            playlists_to_add.extend(mylists)

        if search_albums:
            albums = await self._search_series_by_keyword(search_query, limit)
            albums_to_add.extend(albums)

        # Add items to search result
        if playlists_to_add:
            current_playlists = list(search_result.playlists)
            current_playlists.extend(playlists_to_add)
            search_result.playlists = current_playlists
        if albums_to_add:
            current_albums = list(search_result.albums)
            current_albums.extend(albums_to_add)
            search_result.albums = current_albums

    async def _search_mylists_by_keyword(self, search_query: str, limit: int) -> list[Playlist]:
        """Search for mylists by keyword."""
        list_search_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.search.search_lists,
            search_query,
            page_size=limit,
            types=["mylist"],
        )

        if not list_search_data:
            return []

        playlists = []
        for item in list_search_data.items:
            if isinstance(item, EssentialMylist):
                playlists.append(self.converter_manager.playlist.convert_by_mylist(item))

        return playlists

    async def _search_series_by_keyword(self, search_query: str, limit: int) -> list[Album]:
        """Search for series by keyword."""
        list_search_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.search.search_lists,
            search_query,
            page_size=limit,
            types=["series"],
        )

        if not list_search_data:
            return []

        albums = []
        for item in list_search_data.items:
            if isinstance(item, EssentialSeries):
                albums.append(self.converter_manager.album.convert_by_series(item))

        return albums

    async def search_videos_by_keyword(self, search_query: str, limit: int) -> list[Track]:
        """Search for videos by keyword."""
        video_search_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.search.search_videos_by_keyword,
            search_query,
            page_size=limit,
            search_by_user=True,
        )
        if not video_search_data:
            return []

        tracks = []
        for item in video_search_data.items:
            if item.id_:
                track = self.converter_manager.track.convert_by_essential_video(item)
                if track:
                    tracks.append(track)
        return tracks
