"""Tests for cache controller."""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import aiofiles
import pytest

from music_assistant.constants import DB_TABLE_CACHE, DB_TABLE_SETTINGS, VACUUM_MIN_RECLAIM_RATIO
from music_assistant.controllers.cache import MAX_CACHE_DB_SIZE_MB, CacheController
from music_assistant.controllers.cache.constants import DB_SCHEMA_VERSION, SWR_FALLBACK_MAX_AGE
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.mass import MusicAssistant


@dataclass
class _FakeModel:
    """Simple model for testing base_class reconstruction."""

    name: str = ""
    value: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _FakeModel:
        """Reconstruct from dict."""
        return cls(name=data.get("name", ""), value=data.get("value", 0))


async def _create_db_files(cache_path: str) -> list[str]:
    """
    Create small cache.db, cache.db-wal, and cache.db-shm files.

    :param cache_path: Path to the cache directory.
    """
    db_path = os.path.join(cache_path, "cache.db")
    paths = [db_path + suffix for suffix in ("", "-wal", "-shm")]
    for path in paths:
        async with aiofiles.open(path, "wb") as f:
            await f.write(b"\0")
    return paths


# --- Core get/set behavior ---


async def test_set_and_get_string(cache: CacheController) -> None:
    """Test storing and retrieving a string value."""
    await cache.set("test_key", "hello", provider="test")
    result = await cache.get("test_key", provider="test")
    assert result == "hello"


async def test_get_expiration(cache: CacheController) -> None:
    """get_expiration returns the stored expiry epoch, also for expired rows."""
    before = int(time.time())
    await cache.set("exp_key", {"a": 1}, provider="test", expiration=500)
    expires = await cache.get_expiration("exp_key", provider="test")
    assert expires is not None
    assert abs(expires - (before + 500)) <= 5
    # a missing key yields None
    assert await cache.get_expiration("missing_key", provider="test") is None
    # an expired-but-present row still reports its (past) expiration
    await cache.set("expired_key", "x", provider="test", expiration=-100)
    expired = await cache.get_expiration("expired_key", provider="test")
    assert expired is not None
    assert expired < int(time.time())


async def test_set_and_get_int(cache: CacheController) -> None:
    """Test storing and retrieving an integer value."""
    await cache.set("num", 42, provider="test")
    result = await cache.get("num", provider="test")
    assert result == 42


async def test_set_and_get_float(cache: CacheController) -> None:
    """Test storing and retrieving a float value."""
    await cache.set("pi", 3.14, provider="test")
    result = await cache.get("pi", provider="test")
    assert result == pytest.approx(3.14)


async def test_set_and_get_bool(cache: CacheController) -> None:
    """Test storing and retrieving a boolean value."""
    await cache.set("flag", True, provider="test")
    result = await cache.get("flag", provider="test")
    assert result is True


async def test_set_and_get_none(cache: CacheController) -> None:
    """Test storing and retrieving None."""
    await cache.set("empty", None, provider="test")
    result = await cache.get("empty", provider="test", default="MISSING")
    assert result is None


async def test_set_and_get_dict(cache: CacheController) -> None:
    """Test storing and retrieving a dict value."""
    data = {"name": "test", "count": 5, "nested": {"a": 1}}
    await cache.set("dict_key", data, provider="test")
    result = await cache.get("dict_key", provider="test")
    assert result == data


async def test_set_and_get_list(cache: CacheController) -> None:
    """Test storing and retrieving a list value."""
    data = [1, "two", 3.0, None, True]
    await cache.set("list_key", data, provider="test")
    result = await cache.get("list_key", provider="test")
    assert result == data


async def test_set_and_get_nested_structure(cache: CacheController) -> None:
    """Test storing and retrieving a deeply nested structure."""
    data = {"items": [{"id": 1, "tags": ["a", "b"]}, {"id": 2, "tags": []}]}
    await cache.set("nested", data, provider="test")
    result = await cache.get("nested", provider="test")
    assert result == data


# --- JSON serialization guarantees ---


async def test_data_always_deserialized_from_json(cache: CacheController) -> None:
    """Test that data is always returned as JSON-deserialized (no Python objects)."""
    await cache.set("list_data", [1, 2, 3], provider="test")
    result = await cache.get("list_data", provider="test")
    assert isinstance(result, list)
    assert result == [1, 2, 3]


async def test_base_class_single_dict(cache: CacheController) -> None:
    """Test that base_class reconstructs a single dict into a model."""
    await cache.set("model", {"name": "test", "value": 42}, provider="test")
    result = await cache.get("model", provider="test", base_class=_FakeModel)
    assert isinstance(result, _FakeModel)
    assert result.name == "test"
    assert result.value == 42


async def test_base_class_list_of_dicts(cache: CacheController) -> None:
    """Test that base_class reconstructs each item in a list of dicts."""
    await cache.set("models", [{"name": "a"}, {"name": "b"}], provider="test")
    result = await cache.get("models", provider="test", base_class=_FakeModel)
    assert isinstance(result, list)
    assert len(result) == 2
    assert all(isinstance(item, _FakeModel) for item in result)
    assert result[0].name == "a"
    assert result[1].name == "b"


async def test_base_class_not_applied_to_none(cache: CacheController) -> None:
    """Test that base_class is not applied when cache returns default."""
    result = await cache.get("nonexistent", provider="test", base_class=_FakeModel)
    assert result is None


async def test_non_serializable_raises(cache: CacheController) -> None:
    """Test that non-serializable data raises on set."""
    with pytest.raises(TypeError):
        await cache.set("bad", object(), provider="test")  # type: ignore[arg-type]


# --- Expiration ---


async def test_expired_cache_returns_default(cache: CacheController) -> None:
    """Test that expired cache entries return the default value."""
    await cache.set("expiring", "data", provider="test", expiration=-1)
    result = await cache.get("expiring", provider="test", default="gone")
    assert result == "gone"


# --- Checksum validation ---


async def test_checksum_match(cache: CacheController) -> None:
    """Test that data is returned when checksum matches."""
    await cache.set("ck", "val", provider="test", checksum="abc")
    result = await cache.get("ck", provider="test", checksum="abc")
    assert result == "val"


async def test_checksum_mismatch(cache: CacheController) -> None:
    """Test that default is returned when checksum doesn't match."""
    await cache.set("ck2", "val", provider="test", checksum="abc")
    result = await cache.get("ck2", provider="test", checksum="xyz", default="nope")
    assert result == "nope"


async def test_checksum_as_int(cache: CacheController) -> None:
    """Test that integer checksums are converted to strings."""
    await cache.set("ck3", "val", provider="test", checksum="123")
    result = await cache.get("ck3", provider="test", checksum=123)
    assert result == "val"


# --- Category and provider isolation ---


async def test_different_providers_isolated(cache: CacheController) -> None:
    """Test that the same key in different providers returns different data."""
    await cache.set("key", "from_a", provider="prov_a")
    await cache.set("key", "from_b", provider="prov_b")
    assert await cache.get("key", provider="prov_a") == "from_a"
    assert await cache.get("key", provider="prov_b") == "from_b"


async def test_different_categories_isolated(cache: CacheController) -> None:
    """Test that the same key in different categories returns different data."""
    await cache.set("key", "cat_1", provider="test", category=1)
    await cache.set("key", "cat_2", provider="test", category=2)
    assert await cache.get("key", provider="test", category=1) == "cat_1"
    assert await cache.get("key", provider="test", category=2) == "cat_2"


# --- Delete and clear ---


async def test_delete_specific_key(cache: CacheController) -> None:
    """Test deleting a specific cache entry."""
    await cache.set("del_me", "data", provider="test")
    await cache.delete("del_me", provider="test")
    result = await cache.get("del_me", provider="test", default="gone")
    assert result == "gone"


async def test_clear_removes_entries(cache: CacheController) -> None:
    """Test that clear removes all non-persistent entries."""
    await cache.set("a", "1", provider="test")
    await cache.set("b", "2", provider="test")
    await cache.clear()
    assert await cache.get("a", provider="test") is None
    assert await cache.get("b", provider="test") is None


async def test_clear_preserves_persistent(cache: CacheController) -> None:
    """Test that clear preserves persistent entries."""
    await cache.set("persist", "keep", provider="test", persistent=True)
    await cache.set("temp", "drop", provider="test")
    await cache.clear()
    assert await cache.get("persist", provider="test") == "keep"
    assert await cache.get("temp", provider="test") is None


async def test_clear_with_provider_filter(cache: CacheController) -> None:
    """Test that clear with provider filter only removes matching entries."""
    await cache.set("k", "v1", provider="spotify")
    await cache.set("k", "v2", provider="tidal")
    await cache.clear(provider_filter="spotify")
    assert await cache.get("k", provider="spotify") is None
    assert await cache.get("k", provider="tidal") == "v2"


# --- Bypass ---


async def test_bypass_cache(cache: CacheController) -> None:
    """Test that the bypass context manager skips cache reads."""
    await cache.set("bypass_key", "data", provider="test")
    async with cache.handle_refresh(bypass=True):
        result = await cache.get("bypass_key", provider="test", default="bypassed")
    assert result == "bypassed"
    # outside bypass, cache should still work
    result = await cache.get("bypass_key", provider="test")
    assert result == "data"


# --- Overwrite ---


async def test_overwrite_existing_key(cache: CacheController) -> None:
    """Test that setting the same key overwrites the previous value."""
    await cache.set("ow", "old", provider="test")
    await cache.set("ow", "new", provider="test")
    assert await cache.get("ow", provider="test") == "new"


# --- Empty key handling ---


async def test_get_with_empty_key_raises(cache: CacheController) -> None:
    """Test that getting with empty key raises."""
    with pytest.raises(AssertionError):
        await cache.get("", provider="test")


async def test_set_with_empty_key_is_noop(cache: CacheController) -> None:
    """Test that setting with empty key is silently ignored."""
    await cache.set("", "data", provider="test")
    # should not raise, just be a no-op


# --- Oversized cache detection ---


async def test_cache_warns_when_exceeding_limit(
    mass_minimal: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that a warning is logged (and files kept) when the db exceeds the limit."""
    cache = mass_minimal.cache
    db_files = await _create_db_files(mass_minimal.cache_path)

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:

        async def _side_effect(func: Callable[..., Any], *args: Any) -> Any:
            if getattr(func, "__name__", "") == "_get_db_size":
                return float(MAX_CACHE_DB_SIZE_MB + 100)
            return func(*args)

        mock_to_thread.side_effect = _side_effect
        await cache._check_oversized_cache()

    assert "exceeds recommended maximum" in caplog.text
    for path in db_files:
        assert os.path.exists(path)


async def test_cache_does_not_warn_when_under_limit(
    mass_minimal: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that no warning is logged when the db is under the limit."""
    cache = mass_minimal.cache
    db_files = await _create_db_files(mass_minimal.cache_path)

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:

        async def _side_effect(func: Callable[..., Any], *args: Any) -> Any:
            if getattr(func, "__name__", "") == "_get_db_size":
                return 1.0
            return func(*args)

        mock_to_thread.side_effect = _side_effect
        await cache._check_oversized_cache()

    assert "exceeds recommended maximum" not in caplog.text
    for path in db_files:
        assert os.path.exists(path)


async def test_all_three_db_files_included_in_size(
    mass_minimal: MusicAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that cache.db, cache.db-wal, and cache.db-shm are all summed for size check."""
    cache = mass_minimal.cache
    db_path = os.path.join(mass_minimal.cache_path, "cache.db")

    for suffix in ("", "-wal", "-shm"):
        async with aiofiles.open(db_path + suffix, "wb") as f:
            await f.write(b"\0" * 100)

    size_threshold_mb = 0.0002
    with patch(
        "music_assistant.controllers.cache.controller.MAX_CACHE_DB_SIZE_MB", size_threshold_mb
    ):
        await cache._check_oversized_cache()

    assert "exceeds recommended maximum" in caplog.text
    for suffix in ("", "-wal", "-shm"):
        assert os.path.exists(db_path + suffix)


# --- allow_expired_cache flag ---


async def test_get_with_allow_expired_cache_returns_expired_data(
    cache: CacheController,
) -> None:
    """Test that get(allow_expired_cache=True) returns data past its expiration."""
    await cache.set("stale", "old_data", provider="test", expiration=-1)
    assert await cache.get("stale", provider="test", default="gone") == "gone"
    assert (
        await cache.get("stale", provider="test", default="gone", allow_expired_cache=True)
        == "old_data"
    )


async def test_get_with_allow_expired_cache_still_returns_default_when_missing(
    cache: CacheController,
) -> None:
    """Test that allow_expired_cache=True still returns default when nothing is cached."""
    result = await cache.get(
        "nonexistent", provider="test", default="gone", allow_expired_cache=True
    )
    assert result == "gone"


async def test_auto_cleanup_removes_expired_entries(cache: CacheController) -> None:
    """Test that auto_cleanup removes expired entries by default."""
    await cache.set("evict", "data", provider="test", expiration=-1)
    await cache.auto_cleanup()
    result = await cache.get("evict", provider="test", default="gone", allow_expired_cache=True)
    assert result == "gone"


async def test_auto_cleanup_keeps_allow_expired_cache_entries(
    cache: CacheController,
) -> None:
    """Test that auto_cleanup keeps expired entries with allow_expired_cache=True."""
    await cache.set("keep", "data", provider="test", expiration=-1, allow_expired_cache=True)
    await cache.auto_cleanup()
    result = await cache.get("keep", provider="test", allow_expired_cache=True)
    assert result == "data"


async def test_auto_cleanup_keeps_fresh_entries(cache: CacheController) -> None:
    """Test that auto_cleanup keeps fresh entries regardless of the flag."""
    await cache.set("alive", "data", provider="test", expiration=3600)
    await cache.auto_cleanup()
    assert await cache.get("alive", provider="test") == "data"


async def test_auto_cleanup_scans_all_records(cache: CacheController) -> None:
    """
    Test that auto_cleanup removes expired entries beyond the row-fetch page size.

    Regression test: cleanup previously fetched rows via the default 500-row limit,
    so large caches kept most of their expired entries forever.
    """
    expired_count = 1200
    for i in range(expired_count):
        await cache.set(f"expired_{i}", "data", provider="test", expiration=-1)
    await cache.set("fresh", "data", provider="test", expiration=3600)

    await cache.auto_cleanup()

    assert cache.database is not None
    assert await cache.database.get_count(DB_TABLE_CACHE) == 1
    assert await cache.get("fresh", provider="test") == "data"


# --- Startup vacuum ---


async def test_setup_skips_vacuum_when_little_reclaimable(
    mass_minimal: MusicAssistant,
) -> None:
    """Test that the startup vacuum is skipped when little space can be reclaimed."""
    cache = mass_minimal.cache
    with (
        patch.object(
            DatabaseConnection,
            "get_reclaimable_ratio",
            AsyncMock(return_value=VACUUM_MIN_RECLAIM_RATIO / 2),
        ),
        patch.object(DatabaseConnection, "vacuum", AsyncMock()) as mock_vacuum,
    ):
        await cache._setup_database()
    mock_vacuum.assert_not_called()


async def test_setup_runs_vacuum_when_reclaimable(
    mass_minimal: MusicAssistant,
) -> None:
    """Test that the startup vacuum runs when enough space can be reclaimed."""
    cache = mass_minimal.cache
    with (
        patch.object(
            DatabaseConnection,
            "get_reclaimable_ratio",
            AsyncMock(return_value=VACUUM_MIN_RECLAIM_RATIO + 0.1),
        ),
        patch.object(DatabaseConnection, "vacuum", AsyncMock()) as mock_vacuum,
    ):
        await cache._setup_database()
    mock_vacuum.assert_awaited_once_with()


# --- upsert (in-place write) ---


async def test_set_upserts_in_place(cache: CacheController) -> None:
    """Test that overwriting a key updates the row in place instead of replacing it."""
    assert cache.database is not None
    await cache.set("k", "v1", provider="test")
    row = await cache.database.get_row(
        DB_TABLE_CACHE, {"category": 0, "provider": "test", "key": "k"}
    )
    assert row is not None
    row_id = row["id"]

    await cache.set("k", "v2", provider="test")
    assert await cache.get("k", provider="test") == "v2"
    # a single row that kept its id — an INSERT OR REPLACE would delete it and assign a new id
    assert await cache.database.get_count(DB_TABLE_CACHE) == 1
    row = await cache.database.get_row(
        DB_TABLE_CACHE, {"category": 0, "provider": "test", "key": "k"}
    )
    assert row is not None
    assert row["id"] == row_id


# --- secondary indexes ---


async def _index_names(cache: CacheController) -> set[str]:
    """Return the names of all indexes on the cache table."""
    assert cache.database is not None
    rows = await cache.database.get_rows_from_query(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :table",
        {"table": DB_TABLE_CACHE},
    )
    return {str(row["name"]) for row in rows}


async def test_only_key_provider_index_is_created(cache: CacheController) -> None:
    """Test that only the (key, provider) secondary index is created besides the autoindex."""
    names = await _index_names(cache)
    assert f"{DB_TABLE_CACHE}_key_provider_idx" in names
    # the UNIQUE(category, key, provider) constraint provides an autoindex
    assert any(name.startswith("sqlite_autoindex") for name in names)
    # the redundant indexes are not (re)created
    for removed in (
        "category_idx",
        "key_idx",
        "provider_idx",
        "category_key_idx",
        "category_provider_idx",
        "category_key_provider_idx",
    ):
        assert f"{DB_TABLE_CACHE}_{removed}" not in names


async def test_migration_drops_redundant_indexes(mass_minimal: MusicAssistant) -> None:
    """Test that opening a pre-v9 database migrates cleanly and drops the redundant indexes."""
    db_path = os.path.join(mass_minimal.cache_path, "cache.db")
    redundant = (
        ("category_idx", "category"),
        ("key_idx", "key"),
        ("provider_idx", "provider"),
        ("category_key_idx", "category,key"),
        ("category_provider_idx", "category,provider"),
        ("category_key_provider_idx", "category,key,provider"),
        ("key_provider_idx", "key,provider"),
    )
    # build a v8 database with the old (full) index set and one row
    old_db = DatabaseConnection(db_path)
    await old_db.setup()
    await old_db.execute(
        f"CREATE TABLE {DB_TABLE_SETTINGS}(key TEXT PRIMARY KEY, value TEXT, type TEXT)"
    )
    await old_db.execute(
        f"""CREATE TABLE {DB_TABLE_CACHE}(
            [id] INTEGER PRIMARY KEY AUTOINCREMENT,
            [category] INTEGER NOT NULL DEFAULT 0,
            [key] TEXT NOT NULL,
            [provider] TEXT NOT NULL,
            [expires] INTEGER NOT NULL,
            [data] TEXT NULL,
            [checksum] TEXT NULL,
            [persistent] INTEGER NOT NULL DEFAULT 0,
            [allow_expired_cache] INTEGER NOT NULL DEFAULT 0,
            UNIQUE(category, key, provider)
        )"""
    )
    for suffix, columns in redundant:
        await old_db.execute(
            f"CREATE INDEX {DB_TABLE_CACHE}_{suffix} ON {DB_TABLE_CACHE}({columns})"
        )
    await old_db.execute(
        f"INSERT INTO {DB_TABLE_SETTINGS}(key, value, type) VALUES ('version', '8', 'str')"
    )
    await old_db.execute(
        f"INSERT INTO {DB_TABLE_CACHE}(category, key, provider, expires, data) "
        "VALUES (0, 'kept', 'test', 9999999999, '\"payload\"')"
    )
    await old_db.commit()
    await old_db.close()

    # opening the controller runs the migration
    await mass_minimal.cache._setup_database()
    cache = mass_minimal.cache
    assert cache.database is not None

    version_row = await cache.database.get_row(DB_TABLE_SETTINGS, {"key": "version"})
    assert version_row is not None
    assert version_row["value"] == str(DB_SCHEMA_VERSION)

    names = await _index_names(cache)
    assert f"{DB_TABLE_CACHE}_key_provider_idx" in names
    for suffix, _ in redundant[:-1]:  # every index except key_provider is dropped
        assert f"{DB_TABLE_CACHE}_{suffix}" not in names

    # existing data survived the migration
    assert await cache.get("kept", provider="test") == "payload"


# --- stale-while-revalidate cleanup ---


async def test_auto_cleanup_removes_stale_swr_rows(cache: CacheController) -> None:
    """Test that auto_cleanup removes SWR fallback rows expired beyond the grace window."""
    # expired but within the grace window -> kept as fallback
    await cache.set("recent", "data", provider="test", expiration=-1, allow_expired_cache=True)
    # expired well beyond the grace window -> removed
    await cache.set(
        "ancient",
        "data",
        provider="test",
        expiration=-(SWR_FALLBACK_MAX_AGE + 86400),
        allow_expired_cache=True,
    )
    await cache.auto_cleanup()

    assert await cache.get("recent", provider="test", allow_expired_cache=True) == "data"
    assert (
        await cache.get("ancient", provider="test", default="gone", allow_expired_cache=True)
        == "gone"
    )
