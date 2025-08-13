"""Main fixture generation orchestrator."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from tests.providers.nicovideo.constants import (
    DUMMY_DESCRIPTION,
    GENERATED_FIXTURES_DIR,
    SAMPLE_MYLIST_ID,
    SAMPLE_SERIES_ID,
    SAMPLE_USER_ID,
    SAMPLE_VIDEO_ID,
)
from tests.providers.nicovideo.fixtures.scripts.type_manager import (
    FixtureDataProcessor,
    FixtureTypeManager,
)
from tests.providers.nicovideo.helpers import stabilize_counts_for_fixture
from tests.providers.nicovideo.types import (
    FixtureAPIResultOptional,
    FixtureCategory,
)

if TYPE_CHECKING:
    from niconico import NicoNico
    from niconico.objects.nvapi import (
        RelationshipUsersData,
    )


logger = logging.getLogger(__name__)


class FixtureGenerator:
    """Main fixture generation orchestrator with integrated managers."""

    def __init__(self) -> None:
        """Initialize the fixture generator with all necessary managers."""
        self.limit = 1

        # Initialize managers
        self.type_manager = FixtureTypeManager()
        self.data_processor = FixtureDataProcessor()

    async def save_fixture[T: BaseModel, **P](
        self,
        category: FixtureCategory,
        name: str,
        api_call: Callable[P, FixtureAPIResultOptional[T]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> FixtureAPIResultOptional[T]:
        """Save API response as fixture and return the data."""
        try:
            logger.info(f"Fetching {category}/{name}...")

            # Add delay before API call
            await asyncio.sleep(1.0)

            # API call
            response = await asyncio.to_thread(api_call, *args, **kwargs)

            if response is None:
                logger.warning(f"No data returned for {category}/{name}")
                return None

            # If response is a list, truncate to self.limit
            if isinstance(response, list):
                response = response[: self.limit]

            # Stabilize the response data before processing
            response = stabilize_counts_for_fixture(response)

            # Record type mapping for automatic generation
            self.type_manager.record_type_mapping(response, category, name)

            # Convert to JSON serializable format and save
            data = self.data_processor.convert_to_json_serializable(response)

            fixture_path = GENERATED_FIXTURES_DIR / category / f"{name}.json"
            self.data_processor.save_fixture_data(data, fixture_path)

            # Return original response object
            return response

        except ValidationError as e:
            logger.error(f"Validation error for {category}/{name}:")
            detailed_errors = e.errors()
            for error in detailed_errors:
                logger.error(f"  Field: {error.get('loc', 'Unknown')}")
                logger.error(f"  Type: {error.get('type', 'Unknown')}")
                logger.error(f"  Message: {error.get('msg', 'Unknown')}")
                logger.error(f"  Input: {error.get('input', 'Unknown')}")
            logger.error(f"Full validation error: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to fetch {category}/{name}: {e}")
            return None

    async def generate_tracks_fixtures(self, client: NicoNico) -> None:
        """Generate TRACKS category fixtures."""
        logger.info("=== Generating TRACKS fixtures ===")

        # Own videos
        await self.save_fixture(
            "tracks",
            "own_videos",
            client.user.get_own_videos,
        )

        # Individual video retrieval (watch data - used as track details in provider)
        await self.save_fixture(
            "tracks",
            "watch_data",
            client.video.watch.get_watch_data,
            SAMPLE_VIDEO_ID,
        )

        # User video list (specific user's uploaded videos - converts to Track objects)
        await self.save_fixture(
            "tracks",
            "user_videos",
            client.user.get_user_videos,
            str(SAMPLE_USER_ID),
            page=1,
            page_size=self.limit,
        )

    async def generate_playlists_fixtures(self, client: NicoNico) -> None:
        """Generate PLAYLISTS category fixtures."""
        logger.info("=== Generating PLAYLISTS fixtures ===")

        # Own mylists (used as library playlists in provider)
        await self.save_fixture(
            "playlists",
            "own_mylists",
            client.user.get_own_mylists,
        )

        # Following mylists (used as following playlists in provider)
        await self.save_fixture(
            "playlists",
            "following_mylists",
            client.user.get_own_following_mylists,
        )

        # Individual mylist retrieval
        await self.save_fixture(
            "playlists",
            "single_mylist_details",
            client.video.get_mylist,
            str(SAMPLE_MYLIST_ID),
            page_size=self.limit,
            page=1,
        )

    async def generate_albums_fixtures(self, client: NicoNico) -> None:
        """Generate ALBUMS category fixtures."""
        logger.info("=== Generating ALBUMS fixtures ===")

        # Own series (used as library albums in provider)
        await self.save_fixture(
            "albums",
            "own_series",
            client.user.get_own_series_list,
        )

        # User series list (converts to Album objects)
        await self.save_fixture(
            "albums",
            "user_series",
            client.user.get_user_series,
            str(SAMPLE_USER_ID),
            page=1,
            page_size=self.limit,
        )

        # Individual series retrieval
        await self.save_fixture(
            "albums",
            "single_series_details",
            client.video.get_series,
            str(SAMPLE_SERIES_ID),
            page=1,
            page_size=self.limit,
        )

    def get_own_following_users(self, client: NicoNico) -> RelationshipUsersData | None:
        """Get the current user's following users."""
        users_data = client.user.get_own_followings(page_size=self.limit)
        if users_data is None:
            return None
        for user in users_data.items:
            user.description = DUMMY_DESCRIPTION
            user.short_description = DUMMY_DESCRIPTION
            user.stripped_description = DUMMY_DESCRIPTION

        return users_data

    async def generate_artists_fixtures(self, client: NicoNico) -> None:
        """Generate ARTISTS category fixtures."""
        logger.info("=== Generating ARTISTS fixtures ===")

        # Following users (used as library artists in provider)
        await self.save_fixture(
            "artists",
            "following_users",
            self.get_own_following_users,
            client,
        )

        # Test user
        await self.save_fixture(
            "artists",
            "user_details",
            client.user.get_user,
            str(SAMPLE_USER_ID),
        )

    async def generate_search_fixtures(self, client: NicoNico) -> None:
        """Generate SEARCH category fixtures."""
        logger.info("=== Generating SEARCH fixtures ===")

        # Video search
        await self.save_fixture(
            "search",
            "video_search_keyword",
            client.video.search.search_videos_by_keyword,
            "APIテスト68461151-45285955",
            sort_key="registeredAt",
            sort_order="asc",
            page_size=self.limit,
        )

        # Tag search
        await self.save_fixture(
            "search",
            "video_search_tags",
            client.video.search.search_videos_by_tag,
            "APIテストタグ68461151-45285955",
            sort_key="registeredAt",
            sort_order="asc",
            page_size=self.limit,
        )

        # Mylist search
        await self.save_fixture(
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
        await self.save_fixture(
            "search",
            "series_search",
            client.video.search.search_lists,
            "テストシリーズ68461151-527007",
            sort_key="startTime",
            sort_order="asc",
            page_size=self.limit,
            types=["series"],
        )

    async def generate_history_fixtures(self, client: NicoNico) -> None:
        """Generate HISTORY category fixtures."""
        logger.info("=== Generating HISTORY fixtures ===")

        # History
        await self.save_fixture(
            "history",
            "user_history",
            client.video.get_history,
            page_size=self.limit,
        )

        # Like history
        await self.save_fixture(
            "history",
            "user_likes",
            client.video.get_like_history,
            page_size=self.limit,
        )

    async def generate_all_fixtures(self, client: NicoNico) -> None:
        """Generate all fixtures using the provided client."""
        logger.info("Starting fixture generation...")

        # Generate fixtures for each category
        await self.generate_tracks_fixtures(client)
        await self.generate_playlists_fixtures(client)
        await self.generate_albums_fixtures(client)
        await self.generate_artists_fixtures(client)
        await self.generate_search_fixtures(client)
        await self.generate_history_fixtures(client)

        # Generate fixture types file
        logger.info("=== Generating fixture types file ===")
        self.type_manager.generate_fixture_types_file()

        logger.info("=== All fixtures generated successfully! ===")
