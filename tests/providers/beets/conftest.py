"""Fixtures for the beets provider tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.beets import BeetsProvider
from music_assistant.providers.beets.library import BeetsLibrary
from music_assistant.providers.beets.parsers import ParseContext
from tests.providers.beets.beets_db import BeetsDb

INSTANCE_ID = "beets--test"


def track_prov_id(beets_id: int) -> str:
    """
    Return the provider id of a beets item.

    :param beets_id: The beets items.id.
    """
    return str(beets_id)


def album_prov_id(beets_id: int) -> str:
    """
    Return the provider id of a beets album.

    :param beets_id: The beets albums.id.
    """
    return str(beets_id)


@pytest.fixture
def beets_db(tmp_path: Path) -> BeetsDb:
    """Return an empty current-schema beets database."""
    return BeetsDb(tmp_path / "library.db")


@pytest.fixture
def legacy_beets_db(tmp_path: Path) -> BeetsDb:
    """Return an empty beets database with the pre-multi-value columns."""
    return BeetsDb(tmp_path / "legacy.db", legacy=True)


@pytest.fixture
def music_dir(tmp_path: Path) -> Path:
    """Return an existing, empty music directory."""
    path = tmp_path / "music"
    path.mkdir()
    return path


@pytest.fixture
async def make_provider(
    beets_db: BeetsDb, music_dir: Path
) -> AsyncGenerator[Callable[..., Awaitable[BeetsProvider]]]:
    """Return a factory for BeetsProviders around the test database with a mocked mass."""
    providers: list[BeetsProvider] = []

    async def _make(
        *,
        db_path: Path | None = None,
        beets_directory: str | None = None,
        open_library: bool = True,
        instance_id: str = INSTANCE_ID,
    ) -> BeetsProvider:
        provider = BeetsProvider.__new__(BeetsProvider)
        provider.manifest = MagicMock(domain="beets")
        provider.config = MagicMock(instance_id=instance_id)
        provider.config.name = "beets"
        provider.logger = MagicMock()
        provider.mass = _mock_mass()
        provider.library = BeetsLibrary(str(db_path or beets_db.path))
        provider.music_directory = str(music_dir)
        provider.beets_directory = beets_directory
        provider._ctx = ParseContext(
            instance_id=instance_id,
            domain="beets",
            music_directory=str(music_dir),
            beets_directory=beets_directory,
        )
        if open_library:
            await provider.library.open()
        providers.append(provider)
        return provider

    yield _make
    for provider in providers:
        await provider.library.close()


def _mock_mass() -> MagicMock:
    """Return a mocked mass whose background tasks really run."""
    mass = MagicMock()

    def _create_task(coro: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        return asyncio.get_running_loop().create_task(coro)

    mass.create_task = MagicMock(side_effect=_create_task)
    mass.music.database.get_rows_from_query = AsyncMock(return_value=[])
    mass.music.tracks.add_item_to_library = AsyncMock(
        side_effect=lambda *_args, **_kwargs: MagicMock(item_id=1, favorite=False)
    )
    mass.music.tracks.set_favorite = AsyncMock()
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.tracks.get_library_items_by_prov_id = AsyncMock(return_value=[])
    mass.music.albums.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.artists.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.tracks.remove_item_from_library = AsyncMock()
    mass.music.tracks.remove_provider_mapping = AsyncMock()
    mass.streams.audio_analysis.set_track_loudness = AsyncMock()
    return mass
