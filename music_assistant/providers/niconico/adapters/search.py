"""Search adapter for NicoNico."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Playlist, Track
from niconico.objects.video.search import (
    EssentialMylist,
    EssentialSeries,
    VideoSearchSortKey,
    VideoSearchSortOrder,
)

from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.parsers import (
    parse_album_by_series,
    parse_playlist_by_mylist,
    parse_track_by_essential_video,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import SearchResults

    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoSearchAdapter(NiconicoBaseAdapter):
    """Handles search related operations for NicoNico."""

    def __init__(self, adapter: NicoNicoMusicAssistantAdapter) -> None:
        """Initialize NiconicoSearchAdapter with reference to parent adapter."""
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
        list_search_data = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_lists,
            search_query,
            page_size=limit,
            types=["mylist"],
        )

        if not list_search_data:
            return []

        playlists = []
        for item in list_search_data.items:
            if isinstance(item, EssentialMylist):
                playlists.append(parse_playlist_by_mylist(self.adapter.provider, item))

        return playlists

    async def _search_series_by_keyword(self, search_query: str, limit: int) -> list[Album]:
        """Search for series by keyword."""
        list_search_data = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_lists,
            search_query,
            page_size=limit,
            types=["series"],
        )

        if not list_search_data:
            return []

        albums = []
        for item in list_search_data.items:
            if isinstance(item, EssentialSeries):
                albums.append(parse_album_by_series(self.adapter.provider, item))

        return albums

    async def search_videos_by_keyword(self, search_query: str, limit: int) -> list[Track]:
        """Search for videos by keyword."""
        video_search_data = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_videos_by_keyword,
            search_query,
            page_size=limit,
            search_by_user=True,
        )
        if not video_search_data:
            return []

        tracks = []
        for item in video_search_data.items:
            if item.id_:
                track = parse_track_by_essential_video(self.adapter.provider, item)
                if track:
                    tracks.append(track)
        return tracks

    async def search_videos_by_tags(
        self,
        tags: list[str],
        limit: int,
        sort: VideoSearchSortKey,
        sort_order: VideoSearchSortOrder,
    ) -> list[Track]:
        """Search for videos by tags with specified sort order."""
        if not tags:
            return []

        tracks = []
        # Search for each tag separately since search_videos_by_tag only accepts one tag
        for tag in tags:
            video_search_data = await self.adapter._call_with_throttler(
                self.adapter.niconico_py_client.video.search.search_videos_by_tag,
                tag,
                page_size=limit,
                sort_key=sort,
                sort_order=sort_order,
                search_by_user=True,
            )

            if video_search_data:
                for item in video_search_data.items:
                    if item.id_:
                        track = parse_track_by_essential_video(self.adapter.provider, item)
                        if track:
                            tracks.append(track)

        return tracks[:limit]  # Limit total results
