"""API type to converter function mappings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mashumaro import DataClassDictMixin
from niconico.objects.nvapi import (
    FollowingMylistsData,
    HistoryData,
    LikeHistoryData,
    ListSearchData,
    OwnVideosData,
    RecommendData,
    RelationshipUsersData,
    SeriesData,
    UserVideosData,
    VideoSearchData,
)
from niconico.objects.user import NicoUser, UserMylistItem, UserSeriesItem
from niconico.objects.video import EssentialVideo, Mylist
from niconico.objects.video.watch import WatchData
from pydantic import BaseModel

from music_assistant.providers.nicovideo.converters.stream import (
    StreamConversionData,
)
from tests.providers.nicovideo.fixture_data.shared_types import StreamFixtureData

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.converters.manager import NicovideoConverterManager


# Type definitions for converter results
type SnapshotableItem = DataClassDictMixin
type ConvertedResult = SnapshotableItem | list[SnapshotableItem] | None


@dataclass(frozen=True)
class APIResponseConverterMapping[T: BaseModel]:
    """Maps API type to converter function."""

    source_type: type[T]
    convert_func: Callable[[T, NicovideoConverterManager], ConvertedResult]


# API type to converter function mappings
API_RESPONSE_CONVERTER_MAPPINGS = (
    # Track Types
    APIResponseConverterMapping(
        source_type=EssentialVideo,
        convert_func=lambda data, cm: cm.track.convert_by_essential_video(data),
    ),
    APIResponseConverterMapping(
        source_type=WatchData,
        convert_func=lambda data, cm: cm.track.convert_by_watch_data(data),
    ),
    APIResponseConverterMapping(
        source_type=UserVideosData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item.essential)) is not None
        ],
    ),
    APIResponseConverterMapping(
        source_type=OwnVideosData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item.essential)) is not None
        ],
    ),
    # Playlist Types
    APIResponseConverterMapping(
        source_type=Mylist,
        convert_func=lambda data, cm: cm.playlist.convert_with_tracks_by_mylist(data),
    ),
    APIResponseConverterMapping(
        source_type=UserMylistItem,
        convert_func=lambda data, cm: cm.playlist.convert_by_mylist(data),
    ),
    APIResponseConverterMapping(
        source_type=FollowingMylistsData,
        convert_func=lambda data, cm: [
            cm.playlist.convert_following_by_mylist(item) for item in data.mylists
        ],
    ),
    # Album Types
    APIResponseConverterMapping(
        source_type=SeriesData,
        convert_func=lambda data, cm: cm.album.convert_series_to_album_with_tracks(data),
    ),
    APIResponseConverterMapping(
        source_type=UserSeriesItem,
        convert_func=lambda data, cm: cm.album.convert_by_series(data),
    ),
    # Artist Types
    APIResponseConverterMapping(
        source_type=RelationshipUsersData,
        convert_func=lambda data, cm: [
            cm.artist.convert_by_owner_or_user(item) for item in data.items
        ],
    ),
    APIResponseConverterMapping(
        source_type=NicoUser,
        convert_func=lambda data, cm: cm.artist.convert_by_owner_or_user(data),
    ),
    # Search Types
    APIResponseConverterMapping(
        source_type=VideoSearchData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item)) is not None
        ],
    ),
    APIResponseConverterMapping(
        source_type=ListSearchData,
        convert_func=lambda data, cm: [
            cm.playlist.convert_by_mylist(item)
            if item.type_ == "mylist"
            else cm.album.convert_by_series(item)
            for item in data.items
        ],
    ),
    # History Types
    APIResponseConverterMapping(
        source_type=HistoryData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item.video)) is not None
        ],
    ),
    APIResponseConverterMapping(
        source_type=LikeHistoryData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item.video)) is not None
        ],
    ),
    # Recommendation Types
    APIResponseConverterMapping(
        source_type=RecommendData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if isinstance(item.content, EssentialVideo)
            and (track := cm.track.convert_by_essential_video(item.content)) is not None
        ],
    ),
    # Stream Types
    APIResponseConverterMapping(
        source_type=StreamConversionData,
        convert_func=lambda data, cm: cm.stream.convert_from_conversion_data(data),
    ),
    APIResponseConverterMapping(
        source_type=StreamFixtureData,
        convert_func=lambda data, cm: cm.stream.convert_from_conversion_data(
            StreamConversionData(
                watch_data=data.watch_data,
                selected_audio=data.selected_audio,
                hls_url="https://example.com/stub.m3u8",
                domand_bid="stub_bid",
                hls_playlist_text=(
                    "#EXTM3U\n"
                    "#EXT-X-VERSION:6\n"
                    "#EXT-X-TARGETDURATION:6\n"
                    "#EXT-X-MEDIA-SEQUENCE:1\n"
                    "#EXT-X-PLAYLIST-TYPE:VOD\n"
                    '#EXT-X-MAP:URI="https://example.com/init.mp4"\n'
                    '#EXT-X-KEY:METHOD=AES-128,URI="https://example.com/key"\n'
                    "#EXTINF:6.0,\n"
                    "segment1.m4s\n"
                    "#EXTINF:6.0,\n"
                    "segment2.m4s\n"
                    "#EXT-X-ENDLIST\n"
                ),
            )
        ),
    ),
)


class APIResponseConverterMappingRegistry:
    """Maps API response types to converter functions."""

    def __init__(self) -> None:
        """Initialize the registry."""
        self._registry: dict[type, APIResponseConverterMapping[BaseModel]] = {}
        for mapping in API_RESPONSE_CONVERTER_MAPPINGS:
            self._registry[mapping.source_type] = cast(
                "APIResponseConverterMapping[BaseModel]", mapping
            )

    def get_by_type(self, source_type: type) -> APIResponseConverterMapping[BaseModel] | None:
        """Get mapping by type with O(1) lookup."""
        return self._registry.get(source_type)
