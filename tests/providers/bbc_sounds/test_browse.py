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
        "stations",
        "stations/bbc_radio_one",
        "stations/bbc_radio_one/2020-01-01",
    ]
)
def station_paths(request: pytest.FixtureRequest, provider_domain: str) -> str:
    """Return a parameterised set of valid station browse paths."""
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
        browse_string = f"{provider_domain}listen_live"
        provider._browse_live = AsyncMock()  # type: ignore[method-assign]
        await provider.browse(browse_string)
        provider._browse_live.assert_awaited_once()

    async def test_valid_station_strings(
        self, provider: BBCSoundsProvider, station_paths: str
    ) -> None:
        """Test valid station paths are dispatched into the right browse branch."""
        provider._browse_stations = AsyncMock()  # type: ignore[method-assign]
        await provider.browse(station_paths)
        provider._browse_stations.assert_awaited_once()

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
