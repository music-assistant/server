"""Tests for cache controller oversized cache detection and reset."""

import os
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import aiofiles
import pytest

from music_assistant.controllers.cache import MAX_CACHE_DB_SIZE_MB
from music_assistant.mass import MusicAssistant


async def _create_db_files(cache_path: str) -> list[str]:
    """Create small cache.db, cache.db-wal, and cache.db-shm files.

    :param cache_path: Path to the cache directory.
    """
    db_path = os.path.join(cache_path, "cache.db")
    paths = [db_path + suffix for suffix in ("", "-wal", "-shm")]
    for path in paths:
        async with aiofiles.open(path, "wb") as f:
            await f.write(b"\0")
    return paths


async def test_cache_reset_when_exceeding_limit(mass_minimal: MusicAssistant) -> None:
    """Test that the cache database is removed when it exceeds MAX_CACHE_DB_SIZE_MB.

    :param mass_minimal: Minimal MusicAssistant instance.
    """
    cache = mass_minimal.cache
    db_files = await _create_db_files(mass_minimal.cache_path)

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:

        async def _side_effect(func: Callable[..., Any], *args: Any) -> Any:
            if getattr(func, "__name__", "") == "_get_db_size":
                return float(MAX_CACHE_DB_SIZE_MB + 100)
            return func(*args)

        mock_to_thread.side_effect = _side_effect
        result = await cache._check_and_reset_oversized_cache()

    assert result is True
    for path in db_files:
        assert not os.path.exists(path)


async def test_cache_not_reset_when_under_limit(mass_minimal: MusicAssistant) -> None:
    """Test that the cache database is kept when it is under MAX_CACHE_DB_SIZE_MB.

    :param mass_minimal: Minimal MusicAssistant instance.
    """
    cache = mass_minimal.cache
    db_files = await _create_db_files(mass_minimal.cache_path)

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:

        async def _side_effect(func: Callable[..., Any], *args: Any) -> Any:
            if getattr(func, "__name__", "") == "_get_db_size":
                return 1.0
            return func(*args)

        mock_to_thread.side_effect = _side_effect
        result = await cache._check_and_reset_oversized_cache()

    assert result is False
    for path in db_files:
        assert os.path.exists(path)


async def test_all_three_db_files_included_in_size(mass_minimal: MusicAssistant) -> None:
    """Test that cache.db, cache.db-wal, and cache.db-shm are all summed for size check.

    :param mass_minimal: Minimal MusicAssistant instance.
    """
    cache = mass_minimal.cache
    db_path = os.path.join(mass_minimal.cache_path, "cache.db")

    # Create 3 files of 100 bytes each (300 bytes total)
    for suffix in ("", "-wal", "-shm"):
        async with aiofiles.open(db_path + suffix, "wb") as f:
            await f.write(b"\0" * 100)

    # Set threshold to ~200 bytes so 2 files pass but 3 files exceed it
    size_threshold_mb = 0.0002
    with patch("music_assistant.controllers.cache.MAX_CACHE_DB_SIZE_MB", size_threshold_mb):
        result = await cache._check_and_reset_oversized_cache()

    # 300 bytes exceeds the ~200 byte threshold, proving all 3 files are summed
    assert result is True
    assert not os.path.exists(db_path)
    assert not os.path.exists(db_path + "-wal")
    assert not os.path.exists(db_path + "-shm")


async def test_skip_migration_when_cache_reset(
    mass_minimal: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that database migration is skipped when the cache was reset.

    :param mass_minimal: Minimal MusicAssistant instance.
    :param caplog: Log capture fixture.
    """
    cache = mass_minimal.cache

    with patch.object(cache, "_check_and_reset_oversized_cache", return_value=True):
        await cache._setup_database()

    assert "Performing database migration" not in caplog.text


async def test_skip_vacuum_when_cache_reset(
    mass_minimal: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that database vacuum is skipped when the cache was reset.

    :param mass_minimal: Minimal MusicAssistant instance.
    :param caplog: Log capture fixture.
    """
    cache = mass_minimal.cache

    with patch.object(cache, "_check_and_reset_oversized_cache", return_value=True):
        await cache._setup_database()

    assert "Compacting database" not in caplog.text
