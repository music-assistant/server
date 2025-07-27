"""Search adapter for NicoNico."""

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import SearchResults
from niconico.objects.video import EssentialVideo
from niconico.objects.video.search import EssentialMylist, EssentialSeries

from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.constants import CONF_SENSITIVE_CONTENTS
from music_assistant.providers.niconico.parsers import (
    parse_album_by_series,
    parse_playlist_by_mylist,
    parse_track_by_essential_video,
)

if TYPE_CHECKING:
    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoSearchAdapter(NiconicoBaseAdapter):
    """Handles search related operations for NicoNico."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
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

        # Determine which types to search for
        types = []
        search_playlists = MediaType.PLAYLIST in media_types
        search_albums = MediaType.ALBUM in media_types

        if search_playlists:
            types.append("mylist")
        if search_albums:
            types.append("series")

        if not types:
            return

        list_search_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_lists,
            search_query,
            page_size=limit,
            types=types,
        )

        if list_search_data:
            playlists_to_add = []
            albums_to_add = []

            item: EssentialMylist | EssentialSeries
            for item in list_search_data.items:
                if isinstance(item, EssentialMylist) and search_playlists:
                    playlists_to_add.append(parse_playlist_by_mylist(self.adapter.provider, item))
                elif isinstance(item, EssentialSeries) and search_albums:
                    albums_to_add.append(parse_album_by_series(self.adapter.provider, item))

            # Add items to search result like search_videos_by_keyword does
            if playlists_to_add:
                current_playlists = list(search_result.playlists)
                current_playlists.extend(playlists_to_add)
                search_result.playlists = current_playlists
            if albums_to_add:
                current_albums = list(search_result.albums)
                current_albums.extend(albums_to_add)
                search_result.albums = current_albums

    async def search_videos_by_keyword(
        self, search_query: str, limit: int, search_result: SearchResults
    ) -> None:
        """Search for videos by keyword."""
        sensitive_content = self.adapter.provider.config.get_value(CONF_SENSITIVE_CONTENTS) or None
        video_search_data = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.search.search_videos_by_keyword,
            search_query,
            page_size=limit,
            sensitive_content=sensitive_content,
        )
        if video_search_data:
            search_result.tracks = list(search_result.tracks)
            for item in video_search_data.items:
                if isinstance(item, EssentialVideo):
                    track = parse_track_by_essential_video(self.adapter.provider, item)
                    if track:
                        search_result.tracks.append(track)
