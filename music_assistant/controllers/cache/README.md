# Cache Controller

This package provides a centralized caching layer backed by SQLite. All data stored in the cache goes through JSON serialization, ensuring consistent behavior regardless of when or how the data is retrieved.

## Responsibilities

- Store and retrieve JSON-serializable data with key/provider/category namespacing.
- Enforce data integrity: only `SerializableType` values (str, int, float, bool, None, list, dict) are accepted. Non-serializable objects raise `TypeError` immediately on write.
- Support expiration, checksums, and persistent entries that survive cache clears.
- Provide a `base_class` parameter on `get()` to automatically reconstruct model objects from cached dicts using `from_dict()`.
- Provide a `use_cache` decorator for transparently caching provider/controller method results with automatic serialization and deserialization based on type annotations.
- Run scheduled cleanup of expired entries.
- Warn when the cache database exceeds the recommended maximum size on startup.

## Package Layout

- `controller.py`: main `CacheController` with get/set/delete/clear operations and database lifecycle.
- `constants.py`: shared constants (`DEFAULT_CACHE_EXPIRATION`, `MAX_CACHE_DB_SIZE_MB`, `DB_SCHEMA_VERSION`) and the `BYPASS_CACHE` context variable.
- `helpers.py`: the `use_cache` decorator for provider/controller methods.

## Design Notes

- There is no in-memory cache layer. SQLite with WAL mode, mmap (30GB), a 64MB page cache, and `synchronous=normal` provides fast enough reads for all hot paths. This eliminates the inconsistency where an in-memory cache would return Python objects while the database returned deserialized dicts.
- All data passes through `json_dumps` on write and `json_loads` on read. This means callers must use `.to_dict()` before storing model objects and `.from_dict()` (or the `base_class` parameter) after retrieval. Both `cache.get()` and the `use_cache` decorator accept a `base_class` parameter for automatic reconstruction.
- Cache entries are namespaced by `(category, provider, key)`. The `category` is an integer, `provider` and `key` are strings.
- Entries with `persistent=True` survive calls to `clear()` unless `include_persistent=True` is passed.
- Entries with `allow_expired_cache=True` survive the daily auto-cleanup task even after they have expired, so they remain available as fallback data for the stale-while-revalidate path of `@use_cache`. This is independent of `persistent`: `persistent` controls explicit `clear()` calls, `allow_expired_cache` controls auto-cleanup of expired rows.
- The `@use_cache` decorator accepts `allow_expired_cache=True` to enable stale-while-revalidate: an expired entry is returned immediately and a background refresh updates the cache for the next request. The `BYPASS_CACHE` context variable still forces a synchronous re-fetch.
- The `BYPASS_CACHE` context variable, managed through `handle_refresh()`, forces cache misses for the duration of a context — useful for refresh operations.
- A daily cleanup task removes expired entries (unless `allow_expired_cache=True`). Databases that exceed the recommended max size (2GB) are logged with a warning at startup but kept in place.
