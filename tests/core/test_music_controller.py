"""Tests for the music controller."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest

from music_assistant.constants import VACUUM_MIN_RECLAIM_RATIO
from music_assistant.controllers.music import MusicController
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
