"""Fixture data generation handlers for different categories."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tests.providers.nicovideo.constants import (
    SAMPLE_MYLIST_ID,
    SAMPLE_SERIES_ID,
    SAMPLE_USER_ID,
    SAMPLE_VIDEO_ID,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from niconico import NicoNico
    from pydantic import BaseModel

    from tests.providers.nicovideo.types import FixtureAPIResultOptional

logger = logging.getLogger(__name__)


class FixtureDataGenerators:
    """Handles API calls and data generation for different fixture categories."""

    def __init__(self, limit: int = 1) -> None:
        """Initialize with data limit for API responses."""
        self.limit = limit

    async def generate_tracks_fixtures(
        self,
        client: NicoNico,
        save_fixture: Callable[..., Awaitable[FixtureAPIResultOptional[BaseModel]]],
    ) -> None:
        """Generate TRACKS category fixtures."""
        logger.info("=== Generating TRACKS fixtures ===")

        # Own videos
        await save_fixture(
            "tracks",
            "own_videos",
            client.user.get_own_videos,
        )

        # Individual video retrieval (watch data - used as track details in provider)
        await save_fixture(
            "tracks",
            "watch_data",
            client.video.watch.get_watch_data,
            SAMPLE_VIDEO_ID,
        )

        # User video list (specific user's uploaded videos - converts to Track objects)
        await save_fixture(
            "tracks",
            "user_videos",
            client.user.get_user_videos,
            str(SAMPLE_USER_ID),
            page=1,
            page_size=self.limit,
        )

    async def generate_playlists_fixtures(
        self,
        client: NicoNico,
        save_fixture: Callable[..., Awaitable[FixtureAPIResultOptional[BaseModel]]],
    ) -> None:
        """Generate PLAYLISTS category fixtures."""
        logger.info("=== Generating PLAYLISTS fixtures ===")

        # Own mylists (used as library playlists in provider)
        await save_fixture(
            "playlists",
            "own_mylists",
            client.user.get_own_mylists,
        )

        # Following mylists (used as following playlists in provider)
        await save_fixture(
            "playlists",
            "following_mylists",
            client.user.get_own_following_mylists,
        )

        # Individual mylist retrieval
        await save_fixture(
            "playlists",
            "single_mylist_details",
            client.video.get_mylist,
            str(SAMPLE_MYLIST_ID),
            page_size=self.limit,
            page=1,
        )

    async def generate_albums_fixtures(
        self,
        client: NicoNico,
        save_fixture: Callable[..., Awaitable[FixtureAPIResultOptional[BaseModel]]],
    ) -> None:
        """Generate ALBUMS category fixtures."""
        logger.info("=== Generating ALBUMS fixtures ===")

        # Own series (used as library albums in provider)
        await save_fixture(
            "albums",
            "own_series",
            client.user.get_own_series,
        )

        # User series list (converts to Album objects)
        await save_fixture(
            "albums",
            "user_series",
            client.user.get_user_series,
            str(SAMPLE_USER_ID),
            page=1,
            page_size=self.limit,
        )

        # Individual series retrieval
        await save_fixture(
            "albums",
            "single_series_details",
            client.video.get_series,
            str(SAMPLE_SERIES_ID),
            page=1,
            page_size=self.limit,
        )

    async def generate_artists_fixtures(
        self,
        client: NicoNico,
        save_fixture: Callable[..., Awaitable[FixtureAPIResultOptional[BaseModel]]],
    ) -> None:
        """Generate ARTISTS category fixtures."""
        logger.info("=== Generating ARTISTS fixtures ===")

        # Following users (used as library artists in provider)
        await save_fixture(
            "artists", "following_users", client.user.get_own_followings, page_size=self.limit
        )

        # Test user
        await save_fixture(
            "artists",
            "user_details",
            client.user.get_user,
            str(SAMPLE_USER_ID),
        )

    async def generate_search_fixtures(
        self,
        client: NicoNico,
        save_fixture: Callable[..., Awaitable[FixtureAPIResultOptional[BaseModel]]],
    ) -> None:
        """Generate SEARCH category fixtures."""
        logger.info("=== Generating SEARCH fixtures ===")

        # Video search
        await save_fixture(
            "search",
            "video_search_keyword",
            client.video.search.search_videos_by_keyword,
            "APIテスト68461151-45285955",
            sort_key="registeredAt",
            sort_order="asc",
            page_size=self.limit,
        )

        # Tag search
        await save_fixture(
            "search",
            "video_search_tags",
            client.video.search.search_videos_by_tag,
            "APIテストタグ68461151-45285955",
            sort_key="registeredAt",
            sort_order="asc",
            page_size=self.limit,
        )

        # Mylist search
        await save_fixture(
            "search",
            "mylist_search",
            client.video.search.search_lists,
            "テストマイリスト68461151-78597499",
            sort_key="startTime",
            sort_order="asc",
            page_size=self.limit,
            types=["mylist"],
        )

        # Series search
        await save_fixture(
            "search",
            "series_search",
            client.video.search.search_lists,
            "テストシリーズ68461151-527007",
            sort_key="startTime",
            sort_order="asc",
            page_size=self.limit,
            types=["series"],
        )

    async def generate_history_fixtures(
        self,
        client: NicoNico,
        save_fixture: Callable[..., Awaitable[FixtureAPIResultOptional[BaseModel]]],
    ) -> None:
        """Generate HISTORY category fixtures."""
        logger.info("=== Generating HISTORY fixtures ===")

        # History
        await save_fixture(
            "history",
            "user_history",
            client.video.get_history,
            page_size=self.limit,
        )

        # Like history
        await save_fixture(
            "history",
            "user_likes",
            client.video.get_like_history,
            page_size=self.limit,
        )

    async def generate_all_fixtures(
        self,
        client: NicoNico,
        save_fixture: Callable[..., Awaitable[FixtureAPIResultOptional[BaseModel]]],
    ) -> None:
        """Generate all fixtures using the provided client."""
        logger.info("Starting fixture generation...")

        # Generate fixtures for each category
        await self.generate_tracks_fixtures(client, save_fixture)
        await self.generate_playlists_fixtures(client, save_fixture)
        await self.generate_albums_fixtures(client, save_fixture)
        await self.generate_artists_fixtures(client, save_fixture)
        await self.generate_search_fixtures(client, save_fixture)
        await self.generate_history_fixtures(client, save_fixture)

        logger.info("=== All fixtures generated successfully! ===")
