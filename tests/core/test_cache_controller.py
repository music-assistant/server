"""Tests for cache controller."""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import aiofiles
import pytest

from music_assistant.controllers.cache import MAX_CACHE_DB_SIZE_MB, CacheController
from music_assistant.mass import MusicAssistant


@dataclass
class _FakeModel:
    """Simple model for testing base_class reconstruction."""

    name: str = ""
    value: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "_FakeModel":
        """Reconstruct from dict."""
        return cls(name=data.get("name", ""), value=data.get("value", 0))


@pytest.fixture
async def cache(mass_minimal: MusicAssistant) -> CacheController:
    """Return an initialized cache controller."""
    await mass_minimal.cache._setup_database()
    return mass_minimal.cache


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


# --- Core get/set behavior ---


async def test_set_and_get_string(cache: CacheController) -> None:
    """Test storing and retrieving a string value."""
    await cache.set("test_key", "hello", provider="test")
    result = await cache.get("test_key", provider="test")
    assert result == "hello"


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
