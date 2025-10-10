"""Fixture data generation handlers for different categories."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager
from tests.providers.nicovideo.constants import (
    SAMPLE_MYLIST_ID,
    SAMPLE_SERIES_ID,
    SAMPLE_USER_ID,
    SAMPLE_VIDEO_ID,
)

if TYPE_CHECKING:
    from niconico import NicoNico

    from tests.providers.nicovideo.types import FixtureProcessorProtocol

logger = logging.getLogger(__name__)


class FixtureDataGenerators:
    """Handles API calls and data generation for different fixture categories."""

    def __init__(
        self,
        fixture_processor: FixtureProcessorProtocol,
        client: NicoNico,
        service_manager: NicovideoServiceManager,
        limit: int = 1,
    ) -> None:
        """Initialize with fixture saver dependency and data limit for API responses."""
        self.fixture_processor = fixture_processor
        self.client = client
        self.service_manager = service_manager
        self.limit = limit

    async def generate_tracks_fixtures(
        self,
    ) -> None:
        """Generate TRACKS category fixtures."""
        logger.info("=== Generating TRACKS fixtures ===")

        # Own videos
        await self.fixture_processor.process_fixture(
            "tracks",
            "own_videos",
            self.client.user.get_own_videos,
        )

        # Individual video retrieval (watch data - used as track details in provider)
        await self.fixture_processor.process_fixture(
            "tracks",
            "watch_data",
            self.client.video.watch.get_watch_data,
            SAMPLE_VIDEO_ID,
        )

        # User video list (specific user's uploaded videos - converts to Track objects)
        await self.fixture_processor.process_fixture(
            "tracks",
            "user_videos",
            self.client.user.get_user_videos,
            str(SAMPLE_USER_ID),
            page=1,
            page_size=self.limit,
        )

    async def generate_playlists_fixtures(
        self,
    ) -> None:
        """Generate PLAYLISTS category fixtures."""
        logger.info("=== Generating PLAYLISTS fixtures ===")

        # Own mylists (used as library playlists in provider)
        await self.fixture_processor.process_fixture(
            "playlists",
            "own_mylists",
            self.client.user.get_own_mylists,
        )

        # Following mylists (used as following playlists in provider)
        await self.fixture_processor.process_fixture(
            "playlists",
            "following_mylists",
            self.client.user.get_own_following_mylists,
        )

        # Individual mylist retrieval
        await self.fixture_processor.process_fixture(
            "playlists",
            "single_mylist_details",
            self.client.video.get_mylist,
            str(SAMPLE_MYLIST_ID),
            page_size=self.limit,
            page=1,
        )

    async def generate_albums_fixtures(
        self,
    ) -> None:
        """Generate ALBUMS category fixtures."""
        logger.info("=== Generating ALBUMS fixtures ===")

        # Own series (used as library albums in provider)
        await self.fixture_processor.process_fixture(
            "albums",
            "own_series",
            self.client.user.get_own_series,
        )

        # User series list (converts to Album objects)
        await self.fixture_processor.process_fixture(
            "albums",
            "user_series",
            self.client.user.get_user_series,
            str(SAMPLE_USER_ID),
            page=1,
            page_size=self.limit,
        )

        # Individual series retrieval
        await self.fixture_processor.process_fixture(
            "albums",
            "single_series_details",
            self.client.video.get_series,
            str(SAMPLE_SERIES_ID),
            page=1,
            page_size=self.limit,
        )

    async def generate_artists_fixtures(
        self,
    ) -> None:
        """Generate ARTISTS category fixtures."""
        logger.info("=== Generating ARTISTS fixtures ===")

        # Following users (used as library artists in provider)
        await self.fixture_processor.process_fixture(
            "artists", "following_users", self.client.user.get_own_followings, page_size=self.limit
        )

        # Test user
        await self.fixture_processor.process_fixture(
            "artists",
            "user_details",
            self.client.user.get_user,
            str(SAMPLE_USER_ID),
        )

    async def generate_search_fixtures(
        self,
    ) -> None:
        """Generate SEARCH category fixtures."""
        logger.info("=== Generating SEARCH fixtures ===")

        # Video search
        await self.fixture_processor.process_fixture(
            "search",
            "video_search_keyword",
            self.client.video.search.search_videos_by_keyword,
            "APIテスト68461151-45285955",
            sort_key="registeredAt",
            sort_order="asc",
            page_size=self.limit,
        )

        # Tag search
        await self.fixture_processor.process_fixture(
            "search",
            "video_search_tags",
            self.client.video.search.search_videos_by_tag,
            "APIテストタグ68461151-45285955",
            sort_key="registeredAt",
            sort_order="asc",
            page_size=self.limit,
        )

        # Mylist search
        await self.fixture_processor.process_fixture(
            "search",
            "mylist_search",
            self.client.video.search.search_lists,
            "テストマイリスト68461151-78597499",
            sort_key="startTime",
            sort_order="asc",
            page_size=self.limit,
            types=["mylist"],
        )

        # Series search
        await self.fixture_processor.process_fixture(
            "search",
            "series_search",
            self.client.video.search.search_lists,
            "テストシリーズ68461151-527007",
            sort_key="startTime",
            sort_order="asc",
            page_size=self.limit,
            types=["series"],
        )

    async def generate_history_fixtures(
        self,
    ) -> None:
        """Generate HISTORY category fixtures."""
        logger.info("=== Generating HISTORY fixtures ===")

        # History
        await self.fixture_processor.process_fixture(
            "history",
            "user_history",
            self.client.video.get_history,
            page_size=self.limit,
        )

        # Like history
        await self.fixture_processor.process_fixture(
            "history",
            "user_likes",
            self.client.video.get_like_history,
            page_size=self.limit,
        )

    async def generate_stream_fixtures(
        self,
    ) -> None:
        """Generate STREAM category fixtures."""
        logger.info("=== Generating STREAM fixtures ===")

        # Stream details
        # Note: Using private method for test fixture generation
        # to obtain StreamConversionData directly
        await self.fixture_processor.process_fixture(
            "stream",
            "stream_data",
            self.service_manager.video._prepare_conversion_data,
            SAMPLE_VIDEO_ID,
        )

    async def generate_all_fixtures(
        self,
    ) -> None:
        """Generate all fixtures using the provided client."""
        logger.info("Starting fixture generation...")

        # Generate fixtures for each category
        await self.generate_tracks_fixtures()
        await self.generate_playlists_fixtures()
        await self.generate_albums_fixtures()
        await self.generate_artists_fixtures()
        await self.generate_search_fixtures()
        await self.generate_history_fixtures()
        await self.generate_stream_fixtures()

        logger.info("=== All fixtures generated successfully! ===")
