"""Fixture type mappings for automatic deserialization."""

from __future__ import annotations

from typing import TYPE_CHECKING

# Import all necessary types for fixture mapping
from niconico.objects.nvapi import (
    FollowingMylistsData,
    HistoryData,
    LikeHistoryData,
    ListSearchData,
    OwnVideosData,
    RelationshipUsersData,
    SeriesData,
    UserVideosData,
    VideoSearchData,
)
from niconico.objects.user import NicoUser, UserMylistItem, UserSeriesItem
from niconico.objects.video import Mylist
from niconico.objects.video.watch import WatchData

if TYPE_CHECKING:
    from pydantic import BaseModel

# Fixture type mappings: path -> type
FIXTURE_TYPE_MAPPINGS: dict[str, type[BaseModel]] = {
    "albums/own_series.json": UserSeriesItem,
    "albums/single_series_details.json": SeriesData,
    "albums/user_series.json": UserSeriesItem,
    "artists/following_users.json": RelationshipUsersData,
    "artists/user_details.json": NicoUser,
    "history/user_history.json": HistoryData,
    "history/user_likes.json": LikeHistoryData,
    "playlists/following_mylists.json": FollowingMylistsData,
    "playlists/own_mylists.json": UserMylistItem,
    "playlists/single_mylist_details.json": Mylist,
    "search/mylist_search.json": ListSearchData,
    "search/series_search.json": ListSearchData,
    "search/video_search_keyword.json": VideoSearchData,
    "search/video_search_tags.json": VideoSearchData,
    "tracks/own_videos.json": OwnVideosData,
    "tracks/user_videos.json": UserVideosData,
    "tracks/watch_data.json": WatchData,
}
