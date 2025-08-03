"""Search adapter for NicoNico."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from music_assistant_models.enums import MediaType

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Playlist
from niconico.objects.video.search import EssentialMylist, EssentialSeries

from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.parsers import (
    parse_album_by_series,
    parse_playlist_by_mylist,
    parse_track_by_snapshot_item,
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
        list_search_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_lists,
            search_query,
            page_size=limit,
            types=cast("list[Literal['mylist', 'series']]", ["mylist"]),
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
        list_search_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_lists,
            search_query,
            page_size=limit,
            types=cast("list[Literal['mylist', 'series']]", ["series"]),
        )

        if not list_search_data:
            return []

        albums = []
        for item in list_search_data.items:
            if isinstance(item, EssentialSeries):
                albums.append(parse_album_by_series(self.adapter.provider, item))

        return albums

    async def search_videos_by_keyword(
        self, search_query: str, limit: int, search_result: SearchResults
    ) -> None:
        """Search for videos by keyword."""
        video_search_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_videos_snapshot,
            search_query,
            ["title", "description", "tags"],
            "startTime",
            fields=[
                "contentId",
                "title",
                "description",
                "viewCounter",
                "mylistCounter",
                "likeCounter",
                "startTime",
                "thumbnailUrl",
            ],
            _limit=limit,
        )
        if video_search_data:
            search_result.tracks = list(search_result.tracks)
            for item in video_search_data.data:
                if item.content_id:
                    track = parse_track_by_snapshot_item(self.adapter.provider, item)
                    if track:
                        search_result.tracks.append(track)
