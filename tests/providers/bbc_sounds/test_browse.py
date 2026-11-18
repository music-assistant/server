"""Tests for the path browsing functionality of the BBC Sounds provider."""

import re
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.bbc_sounds import BBCSoundsProvider


@pytest.fixture
def path_prefix(provider: BBCSoundsProvider) -> str:
    """Return the browse path prefix for this provider instance."""
    return f"{provider.instance_id}://"


@pytest.fixture(params=["bbc_sounds://", "bbc_sounds--other://"])
def invalid_path_prefix(request: pytest.FixtureRequest) -> str:
    """Return a prefix for the bare provider domain or another instance."""
    return str(request.param)


@pytest.fixture(
    params=[
        "schedules/bbc_radio_one",
        "schedules/bbc_radio_one/2020-01-01",
    ]
)
def schedule_paths(request: pytest.FixtureRequest, path_prefix: str) -> str:
    """Return a parameterised set of valid paths for a schedule menu or one of its dates."""
    return f"{path_prefix}{request.param}"


@pytest.fixture(
    params=[
        "catch_up/bbc_radio_one",
        "catch_up/bbc_radio_one/2020-01-01",
    ]
)
def catch_up_paths(request: pytest.FixtureRequest, path_prefix: str) -> str:
    """Return a parameterised set of valid paths for a catch-up menu or one of its dates."""
    return f"{path_prefix}{request.param}"


@pytest.fixture(
    params=[
        "catch_up",
        "schedules",
    ]
)
def station_menu_paths(request: pytest.FixtureRequest, path_prefix: str) -> str:
    """Return the catch-up and schedule station lists, which come from the menu."""
    return f"{path_prefix}{request.param}"


@pytest.fixture(
    params=[
        "collections",
        "collections/pid",
    ]
)
def collection_paths(request: pytest.FixtureRequest, path_prefix: str) -> str:
    """Return a parameterised set of valid collection browse paths."""
    return f"{path_prefix}{request.param}"


@pytest.fixture(
    params=[
        "explore",
        "explore/podcasts",
        "explore/podcasts/playlists",
    ]
)
def explore_paths(request: pytest.FixtureRequest, path_prefix: str) -> str:
    """Return a parameterised set of valid explore browse paths."""
    return f"{path_prefix}{request.param}"


@pytest.fixture(
    params=[
        "categories",
        "categories/comedy",
    ]
)
def category_paths(request: pytest.FixtureRequest, path_prefix: str) -> str:
    """Return a parameterised set of valid station category paths."""
    return f"{path_prefix}{request.param}"


@pytest.fixture(
    params=[
        "category",
        "station",
    ]
)
def invalid_paths(request: pytest.FixtureRequest, path_prefix: str) -> str:
    """Return a parameterised set of invalid browse paths."""
    return f"{path_prefix}{request.param}"


class TestBrowse:
    """Test the browse handling functionality."""

    async def test_valid_main_menu_string(
        self, provider: BBCSoundsProvider, path_prefix: str
    ) -> None:
        """Test valid main menu paths are dispatched into the right browse branch."""
        provider.browse_manager._browse_menu = AsyncMock()  # type: ignore[method-assign]
        browse_string = path_prefix
        await provider.browse_manager.browse(browse_string)
        provider.browse_manager._browse_menu.assert_awaited_once()

    async def test_valid_live_string(self, provider: BBCSoundsProvider, path_prefix: str) -> None:
        """Test valid listen live paths are dispatched into the right browse branch."""
        browse_string = f"{path_prefix}live"
        provider.browse_manager._browse_live = AsyncMock()  # type: ignore[method-assign]
        await provider.browse_manager.browse(browse_string)
        provider.browse_manager._browse_live.assert_awaited_once()

    async def test_valid_schedule_strings(
        self, provider: BBCSoundsProvider, schedule_paths: str
    ) -> None:
        """Test valid station paths are dispatched into the right browse branch."""
        provider.browse_manager._browse_schedules = AsyncMock()  # type: ignore[method-assign]
        await provider.browse_manager.browse(schedule_paths)
        provider.browse_manager._browse_schedules.assert_awaited_once()

    async def test_valid_catch_up_strings(
        self, provider: BBCSoundsProvider, catch_up_paths: str
    ) -> None:
        """Test valid catch-up paths are dispatched into the right browse branch."""
        provider.browse_manager._browse_catch_up = AsyncMock()  # type: ignore[method-assign]
        await provider.browse_manager.browse(catch_up_paths)
        provider.browse_manager._browse_catch_up.assert_awaited_once()

    async def test_valid_category_strings(
        self, provider: BBCSoundsProvider, category_paths: str
    ) -> None:
        """Test valid category paths are dispatched into the right browse branch."""
        provider.browse_manager._browse_categories = AsyncMock()  # type: ignore[method-assign]
        await provider.browse_manager.browse(category_paths)
        provider.browse_manager._browse_categories.assert_awaited_once()

    async def test_valid_collection_strings(
        self, provider: BBCSoundsProvider, collection_paths: str
    ) -> None:
        """Test valid collection paths are dispatched into the right browse branch."""
        provider.browse_manager._browse_collections = AsyncMock()  # type: ignore[method-assign]
        await provider.browse_manager.browse(collection_paths)
        provider.browse_manager._browse_collections.assert_awaited_once()

    async def test_invalid_prefix(
        self, provider: BBCSoundsProvider, invalid_path_prefix: str
    ) -> None:
        """Test a path not addressed to this instance raises an exception."""
        message = f"Invalid path for {provider.instance_id} provider: {invalid_path_prefix}"
        with pytest.raises(MusicAssistantError, match=re.escape(message)):
            await provider.browse_manager.browse(invalid_path_prefix)

    async def test_invalid_collection_strings(
        self, provider: BBCSoundsProvider, invalid_paths: str
    ) -> None:
        """Test invalid dispatch paths raise an exception."""
        with pytest.raises(KeyError, match="Invalid subpath"):
            await provider.browse_manager.browse(invalid_paths)

    async def test_playlist_path_is_handled_as_a_playlist(
        self, provider: BBCSoundsProvider, path_prefix: str
    ) -> None:
        """
        Test the playlist browse handling.

        Check a direct link (via Top Picks mainly) is handled by the correct playlist handler.
        """
        provider.browse_manager._get_playlist = AsyncMock()  # type: ignore[method-assign]
        await provider.browse_manager.browse(f"{path_prefix}playlists/pid")
        provider.browse_manager._get_playlist.assert_awaited_once_with("pid")
