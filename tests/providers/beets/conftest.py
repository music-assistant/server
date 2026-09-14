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
        favorite_rating_threshold: float | None = None,
        open_library: bool = True,
    ) -> BeetsProvider:
        provider = BeetsProvider.__new__(BeetsProvider)
        provider.manifest = MagicMock(domain="beets")
        provider.config = MagicMock(instance_id=INSTANCE_ID)
        provider.config.name = "beets"
        provider.config.get_value = MagicMock(return_value=favorite_rating_threshold)
        provider.logger = MagicMock()
        provider.mass = _mock_mass()
        provider.library = BeetsLibrary(str(db_path or beets_db.path))
        provider.music_directory = str(music_dir)
        provider.beets_directory = beets_directory
        provider.sync_running = False
        provider._ctx = ParseContext(
            instance_id=INSTANCE_ID,
            domain="beets",
            music_directory=str(music_dir),
            beets_directory=beets_directory,
            favorite_rating_threshold=favorite_rating_threshold,
        )
        if open_library:
            await provider.library.open()
        providers.append(provider)
        return provider

    yield _make
    for provider in providers:
        await provider.library.close()


def _mock_mass() -> MagicMock:
    """Return a mocked mass whose TaskManager tasks really run."""
    mass = MagicMock()

    def _create_task(coro: Any) -> asyncio.Task[Any]:
        return asyncio.get_running_loop().create_task(coro)

    mass.create_task = MagicMock(side_effect=_create_task)
    mass.music.database.get_rows_from_query = AsyncMock(return_value=[])
    mass.music.tracks.add_item_to_library = AsyncMock(
        side_effect=lambda *_args, **_kwargs: MagicMock(item_id=1, favorite=False)
    )
    mass.music.tracks.set_favorite = AsyncMock()
    mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    mass.music.tracks.remove_item_from_library = AsyncMock()
    mass.music.tracks.remove_provider_mapping = AsyncMock()
    mass.streams.audio_analysis.set_track_loudness = AsyncMock()
    return mass
