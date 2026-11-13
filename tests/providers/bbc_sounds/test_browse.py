"""Tests for the path browsing functionality of the BBC Sounds provider."""

from unittest.mock import AsyncMock

import pytest
from music_assistant_models.errors import MusicAssistantError

from music_assistant.providers.bbc_sounds import BBCSoundsProvider


@pytest.fixture
def provider_domain() -> str:
    """Return the valid provider domain."""
    return "bbc_sounds://"


@pytest.fixture
def incorrect_provider_domain() -> str:
    """Return an invalid provider domain."""
    return "bbc_sound://"


@pytest.fixture(
    params=[
        "schedules/bbc_radio_one",
        "schedules/bbc_radio_one/2020-01-01",
    ]
)
def schedule_paths(request: pytest.FixtureRequest, provider_domain: str) -> str:
    """Return a parameterised set of valid paths for a schedule menu or one of its dates."""
    return f"{provider_domain}{request.param}"


@pytest.fixture(
    params=[
        "catch_up/bbc_radio_one",
        "catch_up/bbc_radio_one/2020-01-01",
    ]
)
def catch_up_paths(request: pytest.FixtureRequest, provider_domain: str) -> str:
    """Return a parameterised set of valid paths for a catch-up menu or one of its dates."""
    return f"{provider_domain}{request.param}"


@pytest.fixture(
    params=[
        "catch_up",
        "schedules",
    ]
)
def station_menu_paths(request: pytest.FixtureRequest, provider_domain: str) -> str:
    """Return the catch-up and schedule station lists, which come from the menu."""
    return f"{provider_domain}{request.param}"


@pytest.fixture(
    params=[
        "collections",
        "collections/pid",
    ]
)
def collection_paths(request: pytest.FixtureRequest, provider_domain: str) -> str:
    """Return a parameterised set of valid collection browse paths."""
    return f"{provider_domain}{request.param}"


@pytest.fixture(
    params=[
        "explore",
        "explore/podcasts",
        "explore/podcasts/playlists",
    ]
)
def explore_paths(request: pytest.FixtureRequest, provider_domain: str) -> str:
    """Return a parameterised set of valid explore browse paths."""
    return f"{provider_domain}{request.param}"


@pytest.fixture(
    params=[
        "categories",
        "categories/comedy",
    ]
)
def category_paths(request: pytest.FixtureRequest, provider_domain: str) -> str:
    """Return a parameterised set of valid station category paths."""
    return f"{provider_domain}{request.param}"


@pytest.fixture(
    params=[
        "category",
        "station",
    ]
)
def invalid_paths(request: pytest.FixtureRequest, provider_domain: str) -> str:
    """Return a parameterised set of invalid browse paths."""
    return f"{provider_domain}{request.param}"


class TestBrowse:
    """Test the browse handling functionality."""

    async def test_valid_main_menu_string(
        self, provider: BBCSoundsProvider, provider_domain: str
    ) -> None:
        """Test valid main menu paths are dispatched into the right browse branch."""
        provider._browse_menu = AsyncMock()  # type: ignore[method-assign]
        browse_string = provider_domain
        await provider.browse(browse_string)
        provider._browse_menu.assert_awaited_once()

    async def test_valid_live_string(
        self, provider: BBCSoundsProvider, provider_domain: str
    ) -> None:
        """Test valid listen live paths are dispatched into the right browse branch."""
        browse_string = f"{provider_domain}live"
        provider._browse_live = AsyncMock()  # type: ignore[method-assign]
        await provider.browse(browse_string)
        provider._browse_live.assert_awaited_once()

    async def test_valid_schedule_strings(
        self, provider: BBCSoundsProvider, schedule_paths: str
    ) -> None:
        """Test valid station paths are dispatched into the right browse branch."""
        provider._browse_schedules = AsyncMock()  # type: ignore[method-assign]
        await provider.browse(schedule_paths)
        provider._browse_schedules.assert_awaited_once()

    async def test_valid_catch_up_strings(
        self, provider: BBCSoundsProvider, catch_up_paths: str
    ) -> None:
        """Test valid catch-up paths are dispatched into the right browse branch."""
        provider._browse_catch_up = AsyncMock()  # type: ignore[method-assign]
        await provider.browse(catch_up_paths)
        provider._browse_catch_up.assert_awaited_once()

    async def test_valid_category_strings(
        self, provider: BBCSoundsProvider, category_paths: str
    ) -> None:
        """Test valid category paths are dispatched into the right browse branch."""
        provider._browse_categories = AsyncMock()  # type: ignore[method-assign]
        await provider.browse(category_paths)
        provider._browse_categories.assert_awaited_once()

    async def test_valid_collection_strings(
        self, provider: BBCSoundsProvider, collection_paths: str
    ) -> None:
        """Test valid collection paths are dispatched into the right browse branch."""
        provider._browse_collections = AsyncMock()  # type: ignore[method-assign]
        await provider.browse(collection_paths)
        provider._browse_collections.assert_awaited_once()

    async def test_invalid_domain(
        self, provider: BBCSoundsProvider, incorrect_provider_domain: str
    ) -> None:
        """Test an invalid raises an exception."""
        with pytest.raises(
            MusicAssistantError,
            match=f"Invalid path for bbc_sounds provider: {incorrect_provider_domain}",
        ):
            await provider.browse(incorrect_provider_domain)

    async def test_invalid_collection_strings(
        self, provider: BBCSoundsProvider, invalid_paths: str
    ) -> None:
        """Test invalid dispatch paths raise an exception."""
        with pytest.raises(KeyError, match="Invalid subpath"):
            await provider.browse(invalid_paths)

    async def test_playlist_path_is_handled_as_a_playlist(
        self, provider: BBCSoundsProvider, provider_domain: str
    ) -> None:
        """
        Test the playlist browse handling.

        Check a direct link (via Top Picks mainly) is handled by the correct playlist handler.
        """
        provider._get_playlist = AsyncMock()  # type: ignore[method-assign]
        await provider.browse(f"{provider_domain}playlists/pid")
        provider._get_playlist.assert_awaited_once_with("pid")
