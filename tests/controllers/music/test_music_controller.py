"""Tests for the music controller."""

import sqlite3
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import MusicAssistantError, UnsupportedFeaturedException
from music_assistant_models.media_items import SoundEffect

from music_assistant.constants import VACUUM_MIN_RECLAIM_RATIO
from music_assistant.controllers.music import MusicController
from music_assistant.controllers.music.database import MusicDatabaseSetupMixin
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.mass import MusicAssistant


@pytest.fixture
async def music(mass_minimal: MusicAssistant) -> AsyncGenerator[MusicController]:
    """Return a music controller attached to the minimal mass instance."""
    controller = MusicController(mass_minimal)
    mass_minimal.music = controller
    yield controller
    # close the db connection so its worker thread does not outlive the test
    if controller._database:
        await controller._database.close()


async def test_setup_fails_clearly_without_fts5_support(music: MusicController) -> None:
    """Missing FTS5 trigram support surfaces as a clear error instead of a raw SQL error."""
    orig_execute = DatabaseConnection.execute

    async def fake_execute(
        self: DatabaseConnection, query: str, values: dict[str, Any] | None = None
    ) -> Any:
        if "USING fts5" in query:
            raise sqlite3.OperationalError("no such tokenizer: trigram")
        return await orig_execute(self, query, values)

    with (
        patch.object(DatabaseConnection, "execute", fake_execute),
        pytest.raises(MusicAssistantError, match=r"SQLite 3\.34"),
    ):
        await music._setup_database()


async def test_setup_skips_vacuum_when_little_reclaimable(music: MusicController) -> None:
    """Test that the library db startup vacuum is skipped when little can be reclaimed."""
    with (
        patch.object(
            DatabaseConnection,
            "get_reclaimable_ratio",
            AsyncMock(return_value=VACUUM_MIN_RECLAIM_RATIO / 2),
        ),
        patch.object(DatabaseConnection, "vacuum", AsyncMock()) as mock_vacuum,
    ):
        await music._setup_database()
    mock_vacuum.assert_not_called()


async def test_setup_runs_vacuum_when_reclaimable(music: MusicController) -> None:
    """Test that the library db startup vacuum runs when enough space can be reclaimed."""
    with (
        patch.object(
            DatabaseConnection,
            "get_reclaimable_ratio",
            AsyncMock(return_value=VACUUM_MIN_RECLAIM_RATIO + 0.1),
        ),
        patch.object(DatabaseConnection, "vacuum", AsyncMock()) as mock_vacuum,
    ):
        await music._setup_database()
    mock_vacuum.assert_awaited_once_with()


def test_database_setup_mixin_applied() -> None:
    """The database setup/migration logic is provided to the controller via the mixin."""
    assert issubclass(MusicController, MusicDatabaseSetupMixin)
    # the schema/migration entrypoints resolve on the controller
    assert hasattr(MusicController, "_setup_database")
    assert hasattr(MusicController, "_reset_database")
    assert hasattr(MusicController, "_cleanup_database")


async def test_cleanup_database_runs_on_empty_library(music: MusicController) -> None:
    """Database maintenance/cleanup completes without error on a freshly created library."""
    await music._setup_database()
    # should be a no-op on an empty database, but must run end-to-end against a real connection
    await music._cleanup_database()


def _sound_effect() -> SoundEffect:
    """Return a minimal SoundEffect item."""
    return SoundEffect(
        item_id="rain.mp3",
        provider="filesystem_local--test",
        name="rain",
        provider_mappings=set(),
    )


async def test_add_to_favorites_rejects_sound_effect() -> None:
    """add_item_to_favorites raises UnsupportedFeaturedException for SOUND_EFFECT."""
    controller = MusicController.__new__(MusicController)
    controller.mass = MagicMock()
    with pytest.raises(UnsupportedFeaturedException, match="can not be favorites"):
        await controller.add_item_to_favorites(_sound_effect())


async def test_add_to_library_rejects_sound_effect() -> None:
    """add_item_to_library raises UnsupportedFeaturedException for SOUND_EFFECT."""
    controller = MusicController.__new__(MusicController)
    controller.mass = MagicMock()
    controller.get_item = AsyncMock(return_value=_sound_effect())  # type: ignore[method-assign]
    with pytest.raises(UnsupportedFeaturedException, match="can not be library items"):
        await controller.add_item_to_library(_sound_effect())


async def test_add_to_favorites_rejects_stale_sound_effect_uri() -> None:
    """A stale sound-effect URI (provider unloaded) raises the honest error."""
    controller = MusicController.__new__(MusicController)
    controller.mass = MagicMock()
    with pytest.raises(UnsupportedFeaturedException, match="can not be favorites"):
        await controller.add_item_to_favorites("stale_provider://sound_effect/rain.mp3")


async def test_add_to_library_rejects_stale_sound_effect_uri() -> None:
    """A stale sound-effect URI (provider unloaded) raises the honest error."""
    controller = MusicController.__new__(MusicController)
    controller.mass = MagicMock()
    with pytest.raises(UnsupportedFeaturedException, match="can not be library items"):
        await controller.add_item_to_library("stale_provider://sound_effect/rain.mp3")
