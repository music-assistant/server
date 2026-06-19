"""Tests for the music library database migrations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MusicAssistantError

from music_assistant.controllers.music.migrations import migrate_database


async def test_migrate_database_rejects_too_old_schema() -> None:
    """Schema versions older than the minimum supported version are refused up-front."""
    create_tables = AsyncMock()
    with pytest.raises(MusicAssistantError):
        await migrate_database(
            MagicMock(),  # mass
            MagicMock(),  # database
            MagicMock(),  # logger
            prev_version=14,
            create_tables=create_tables,
        )
    # the guard fires before any schema work happens
    create_tables.assert_not_awaited()
