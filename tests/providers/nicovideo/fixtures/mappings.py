"""FixtureTestMapping registry and constant definitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

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

if TYPE_CHECKING:
    from music_assistant.providers.nicovideo.converters.manager import NicovideoConverterManager
    from tests.providers.nicovideo.types import FixtureAPIResultOptional


# Type definitions for converter results
type SnapshotableItem = DataClassDictMixin
type ConvertedResult = SnapshotableItem | list[SnapshotableItem] | None


@dataclass(frozen=True)
class FixtureTestMapping[T: BaseModel]:
    """Integrated type test mapping."""

    source_type: type[T]
    convert_func: Callable[[T, NicovideoConverterManager], ConvertedResult]


# Constant mapping definitions - using converter functions directly
FIXTURE_TEST_MAPPINGS: list[FixtureTestMapping[Any]] = [
    # Track Types
    FixtureTestMapping[EssentialVideo](
        source_type=EssentialVideo,
        convert_func=lambda data, cm: cm.track.convert_by_essential_video(data),
    ),
    FixtureTestMapping[WatchData](
        source_type=WatchData,
        convert_func=lambda data, cm: cm.track.convert_by_watch_data(data),
    ),
    FixtureTestMapping[UserVideosData](
        source_type=UserVideosData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item.essential)) is not None
        ],
    ),
    FixtureTestMapping[OwnVideosData](
        source_type=OwnVideosData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item.essential)) is not None
        ],
    ),
    # Playlist Types
    FixtureTestMapping[Mylist](
        source_type=Mylist,
        convert_func=lambda data, cm: cm.playlist.convert_with_tracks_by_mylist(data),
    ),
    FixtureTestMapping[UserMylistItem](
        source_type=UserMylistItem,
        convert_func=lambda data, cm: cm.playlist.convert_by_mylist(data),
    ),
    FixtureTestMapping[FollowingMylistsData](
        source_type=FollowingMylistsData,
        convert_func=lambda data, cm: [
            cm.playlist.convert_following_by_mylist(item) for item in data.mylists
        ],
    ),
    # Album Types
    FixtureTestMapping[SeriesData](
        source_type=SeriesData,
        convert_func=lambda data, cm: cm.album.convert_by_series(data),
    ),
    FixtureTestMapping[UserSeriesItem](
        source_type=UserSeriesItem,
        convert_func=lambda data, cm: cm.album.convert_by_series(data),
    ),
    # Artist Types
    FixtureTestMapping[RelationshipUsersData](
        source_type=RelationshipUsersData,
        convert_func=lambda data, cm: [
            cm.artist.convert_by_owner_or_user(item) for item in data.items
        ],
    ),
    FixtureTestMapping[NicoUser](
        source_type=NicoUser,
        convert_func=lambda data, cm: cm.artist.convert_by_owner_or_user(data),
    ),
    # Search Types
    FixtureTestMapping[VideoSearchData](
        source_type=VideoSearchData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item)) is not None
        ],
    ),
    FixtureTestMapping[ListSearchData](
        source_type=ListSearchData,
        convert_func=lambda data, cm: [
            cm.playlist.convert_by_mylist(item)
            if item.type_ == "mylist"
            else cm.album.convert_by_series(item)
            for item in data.items
        ],
    ),
    # History Types
    FixtureTestMapping[HistoryData](
        source_type=HistoryData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item.video)) is not None
        ],
    ),
    FixtureTestMapping[LikeHistoryData](
        source_type=LikeHistoryData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if (track := cm.track.convert_by_essential_video(item.video)) is not None
        ],
    ),
    # Recommendation Types
    FixtureTestMapping[RecommendData](
        source_type=RecommendData,
        convert_func=lambda data, cm: [
            track
            for item in data.items
            if isinstance(item.content, EssentialVideo)
            and (track := cm.track.convert_by_essential_video(item.content)) is not None
        ],
    ),
]


class FixtureTestMappingRegistry:
    """Type-safe mapping registry."""

    def __init__(self) -> None:
        """Initialize the registry."""
        self._registry: dict[type, FixtureTestMapping[BaseModel]] = {}
        for mapping in FIXTURE_TEST_MAPPINGS:
            self.register(mapping)

    def register[T: BaseModel](self, mapping: FixtureTestMapping[T]) -> None:
        """Register mapping."""
        self._registry[mapping.source_type] = cast("FixtureTestMapping[BaseModel]", mapping)

    def get_by_type(self, source_type: type) -> FixtureTestMapping[BaseModel] | None:
        """O(1) type-based search."""
        return self._registry.get(source_type)

    def get_by_data[T: BaseModel](
        self, api_data: FixtureAPIResultOptional[T]
    ) -> FixtureTestMapping[BaseModel] | None:
        """Get mapping and data type for the API data."""
        if api_data is None:
            return None

        # Determine the type to look up
        if isinstance(api_data, BaseModel):
            lookup_type = type(api_data)
        elif isinstance(api_data, list) and api_data:
            lookup_type = type(api_data[0])
        else:
            return None

        mapping = self.get_by_type(lookup_type)
        if mapping is None:
            return None

        return mapping

    def get_all_mappings(self) -> list[FixtureTestMapping[BaseModel]]:
        """Get all mappings."""
        return list(self._registry.values())
