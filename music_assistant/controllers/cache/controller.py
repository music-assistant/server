"""Cache controller implementation."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import DB_TABLE_CACHE, DB_TABLE_SETTINGS
from music_assistant.controllers.cache.constants import (
    BYPASS_CACHE,
    CACHE_DATABASE_CLEANUP_TASK_ID,
    CONF_CLEAR_CACHE,
    DB_SCHEMA_VERSION,
    DEFAULT_CACHE_EXPIRATION,
    LOGGER,
    MAX_CACHE_DB_SIZE_MB,
    SerializableType,
)
from music_assistant.controllers.tasks.context import (
    update_current_task_progress_from_index,
    update_current_task_progress_text,
)
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.datetime import local_clock_time_to_utc
from music_assistant.helpers.json import async_json_loads, json_dumps
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant import MusicAssistant


class CacheController(CoreController):
    """Controller handling caching of data throughout the application."""

    domain: str = "cache"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
        self.database: DatabaseConnection | None = None
        self.manifest.name = "Cache controller"
        self.manifest.description = (
            "Music Assistant's core controller for caching data throughout the application."
        )
        self.manifest.icon = "memory"

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> tuple[ConfigEntry, ...]:
        """Return all Config Entries for this core module (if any)."""
        if action == CONF_CLEAR_CACHE:
            await self.clear()
            return (
                ConfigEntry(
                    key=CONF_CLEAR_CACHE,
                    type=ConfigEntryType.LABEL,
                    label="The cache has been cleared",
                ),
            )
        return (
            ConfigEntry(
                key=CONF_CLEAR_CACHE,
                type=ConfigEntryType.ACTION,
                label="Clear cache",
                description="Reset/clear all items in the cache. ",
            ),
        )

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of cache module."""
        self.logger.info("Initializing cache controller...")
        await self._setup_database()

    async def post_setup(self) -> None:
        """Handle logic after all core controllers have been set up."""
        self._register_cleanup_task()

    async def close(self) -> None:
        """Cleanup on exit."""
        if self.database:
            await self.database.close()

    async def get(
        self,
        key: str,
        provider: str = "default",
        category: int = 0,
        checksum: str | int | None = None,
        default: Any = None,
        allow_bypass: bool | None = None,
        base_class: Any = None,
        allow_expired_cache: bool = False,
    ) -> Any:
        """
        Get data from cache.

        Returns JSON-deserialized data (dicts, lists, strings, numbers, booleans, None).

        If base_class is provided, the raw data is automatically reconstructed using
        its from_dict() method. If the cached data is a list of dicts, each item is
        reconstructed individually.

        :param key: The (unique) lookup key of the cache object.
        :param provider: Provider id to group cache objects.
        :param category: Category to group cache objects.
        :param checksum: If provided, only return data if the stored checksum matches.
        :param default: Value to return if no cache object is found.
        :param allow_bypass: Whether to respect the BYPASS_CACHE context variable.
        :param base_class: If provided, reconstruct data using base_class.from_dict().
        :param allow_expired_cache: If True, also return entries past their expiration
            time instead of treating them as cache misses. Used by the
            stale-while-revalidate path of `@use_cache`.
        """
        assert self.database is not None
        assert key, "No key provided"
        if allow_bypass and BYPASS_CACHE.get():
            return default
        cur_time = int(time.time())
        if checksum is not None and not isinstance(checksum, str):
            checksum = str(checksum)
        if (
            (
                db_row := await self.database.get_row(
                    DB_TABLE_CACHE, {"category": category, "provider": provider, "key": key}
                )
            )
            and (db_row["expires"] >= cur_time or allow_expired_cache)
            and (not checksum or db_row["checksum"] == checksum)
        ):
            # if allow_bypass is not explicitly set,
            # determine it based on the 'persistent' flag of the cache entry
            if allow_bypass is None:
                allow_bypass = not bool(db_row["persistent"])
            if allow_bypass and BYPASS_CACHE.get():
                return default
            try:
                data = await async_json_loads(db_row["data"])
            except Exception as exc:
                LOGGER.error(
                    "Error parsing cache data for %s/%s/%s: %s",
                    provider,
                    category,
                    key,
                    str(exc),
                    exc_info=exc if self.logger.isEnabledFor(10) else None,
                )
            else:
                if base_class is not None and data is not None:
                    if isinstance(data, list):
                        return [base_class.from_dict(item) for item in data]
                    return base_class.from_dict(data)
                return data
        return default

    async def set(
        self,
        key: str,
        data: SerializableType,
        expiration: int = DEFAULT_CACHE_EXPIRATION,
        provider: str = "default",
        category: int = 0,
        checksum: str | None = None,
        persistent: bool = False,
        allow_expired_cache: bool = False,
    ) -> None:
        """
        Store data in cache.

        Data must be JSON-serializable (str, int, float, bool, None, list, dict).
        Do not pass model objects directly — use .to_dict() first.
        Non-serializable data will raise TypeError.

        :param key: The (unique) lookup key of the cache object.
        :param data: JSON-serializable data to store.
        :param expiration: Time in seconds the cache object should be valid.
        :param provider: Provider id to group cache objects.
        :param category: Category to group cache objects.
        :param checksum: Optional checksum to store with the cache object.
        :param persistent: If True, the entry survives cache clears.
        :param allow_expired_cache: If True, the entry survives the auto-cleanup task
            after it expires, so it can still be served as fallback data by the
            stale-while-revalidate path of `@use_cache`.
        """
        assert self.database is not None
        if not key:
            return
        if checksum is not None:
            checksum = str(checksum)
        expires = int(time.time() + expiration)
        # always serialize to JSON to ensure data is serializable
        # this raises if the data contains non-serializable objects
        data = await asyncio.to_thread(json_dumps, data)
        await self.database.insert_or_replace(
            DB_TABLE_CACHE,
            {
                "category": category,
                "provider": provider,
                "key": key,
                "expires": expires,
                "checksum": checksum,
                "data": data,
                "persistent": persistent,
                "allow_expired_cache": allow_expired_cache,
            },
        )

    async def delete(
        self, key: str | None, category: int | None = None, provider: str | None = None
    ) -> None:
        """Delete data from cache."""
        assert self.database is not None
        match: dict[str, str | int] = {}
        if key is not None:
            match["key"] = key
        if category is not None:
            match["category"] = category
        if provider is not None:
            match["provider"] = provider
        await self.database.delete(DB_TABLE_CACHE, match)

    async def clear(
        self,
        key_filter: str | None = None,
        category_filter: int | None = None,
        provider_filter: str | None = None,
        include_persistent: bool = False,
    ) -> None:
        """Clear all/partial items from cache."""
        assert self.database is not None
        self.logger.info("Clearing database...")
        query_parts: list[str] = []
        if category_filter is not None:
            query_parts.append(f"category = {category_filter}")
        if provider_filter is not None:
            query_parts.append(f"provider LIKE '%{provider_filter}%'")
        if key_filter is not None:
            query_parts.append(f"key LIKE '%{key_filter}%'")
        if not include_persistent:
            query_parts.append("persistent = 0")
        query = "WHERE " + " AND ".join(query_parts) if query_parts else None
        await self.database.delete(DB_TABLE_CACHE, query=query)
        self.logger.info("Clearing database DONE")

    async def auto_cleanup(self) -> None:
        """Run scheduled auto cleanup task."""
        assert self.database is not None
        self.logger.debug("Running automatic cleanup...")
        update_current_task_progress_text("Loading cache records")
        cur_timestamp = int(time.time())
        cleaned_records = 0
        db_rows = await self.database.get_rows(DB_TABLE_CACHE)
        for index, db_row in enumerate(db_rows, 1):
            update_current_task_progress_from_index(
                index,
                len(db_rows),
                f"Scanning cache record {index}/{len(db_rows)}",
            )
            # clean up db cache object only if expired; entries marked with
            # allow_expired_cache are kept as fallback for stale-while-revalidate
            if db_row["expires"] < cur_timestamp and not db_row["allow_expired_cache"]:
                await self.database.delete(DB_TABLE_CACHE, {"id": db_row["id"]})
                cleaned_records += 1
            await asyncio.sleep(0)  # yield to eventloop
        update_current_task_progress_text(f"Cleaned up {cleaned_records} expired cache record(s)")
        self.logger.debug("Automatic cleanup finished (cleaned up %s records)", cleaned_records)

    @asynccontextmanager
    async def handle_refresh(self, bypass: bool) -> AsyncGenerator[None]:
        """Handle the cache bypass."""
        try:
            token = BYPASS_CACHE.set(bypass)
            yield None
        finally:
            BYPASS_CACHE.reset(token)

    async def _check_oversized_cache(self) -> None:
        """Warn if the cache database exceeds the recommended max size."""
        db_path = os.path.join(self.mass.cache_path, "cache.db")
        # also include the write ahead log and shared memory db files
        db_files = [db_path + suffix for suffix in ("", "-wal", "-shm")]

        def _get_db_size() -> float:
            total = 0
            for path in db_files:
                if os.path.exists(path):
                    total += Path(path).stat().st_size
            return total / (1024 * 1024)

        db_size_mb = await asyncio.to_thread(_get_db_size)
        if db_size_mb > MAX_CACHE_DB_SIZE_MB:
            self.logger.warning(
                "Cache database size %.2f MB exceeds recommended maximum of %d MB",
                db_size_mb,
                MAX_CACHE_DB_SIZE_MB,
            )

    async def _setup_database(self) -> None:
        """Initialize database."""
        await self._check_oversized_cache()
        db_path = os.path.join(self.mass.cache_path, "cache.db")
        self.database = DatabaseConnection(db_path)
        await self.database.setup()

        # always create db tables if they don't exist to prevent errors trying to access them later
        await self.__create_database_tables()

        try:
            if db_row := await self.database.get_row(DB_TABLE_SETTINGS, {"key": "version"}):
                prev_version = int(db_row["value"])
            else:
                prev_version = 0
        except (KeyError, ValueError):
            prev_version = 0

        if prev_version not in (0, DB_SCHEMA_VERSION):
            LOGGER.warning(
                "Performing database migration from %s to %s",
                prev_version,
                DB_SCHEMA_VERSION,
            )
            try:
                await self.__migrate_database(prev_version)
            except Exception as err:
                LOGGER.warning("Cache database migration failed: %s, resetting cache", err)
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_CACHE}")
                await self.__create_database_tables()

        # store current schema version
        await self.database.insert_or_replace(
            DB_TABLE_SETTINGS,
            {"key": "version", "value": str(DB_SCHEMA_VERSION), "type": "str"},
        )
        await self.__create_database_indexes()

        # compact db (vacuum) at startup
        self.logger.debug("Compacting database...")
        try:
            await self.database.vacuum()
        except Exception as err:
            self.logger.warning("Database vacuum failed: %s", str(err))
        else:
            self.logger.debug("Compacting database done")

    async def __create_database_tables(self) -> None:
        """Create database table(s)."""
        assert self.database is not None
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_SETTINGS}(
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    type TEXT
                );"""
        )
        await self.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {DB_TABLE_CACHE}(
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

        await self.database.commit()

    async def __create_database_indexes(self) -> None:
        """Create database indexes."""
        assert self.database is not None
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_category_idx "
            f"ON {DB_TABLE_CACHE}(category);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_key_idx ON {DB_TABLE_CACHE}(key);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_provider_idx "
            f"ON {DB_TABLE_CACHE}(provider);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_category_key_idx "
            f"ON {DB_TABLE_CACHE}(category,key);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_category_provider_idx "
            f"ON {DB_TABLE_CACHE}(category,provider);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_category_key_provider_idx "
            f"ON {DB_TABLE_CACHE}(category,key,provider);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_key_provider_idx "
            f"ON {DB_TABLE_CACHE}(key,provider);"
        )
        await self.database.commit()

    async def __migrate_database(self, prev_version: int) -> None:
        """Perform a database migration."""
        assert self.database is not None
        if prev_version <= 6:
            # clear spotify cache entries to fix bloated cache from playlist pagination bug
            await self.database.delete(DB_TABLE_CACHE, query="WHERE provider LIKE '%spotify%'")
        if prev_version <= 7:
            await self.database.execute(
                f"ALTER TABLE {DB_TABLE_CACHE} "
                "ADD COLUMN allow_expired_cache INTEGER NOT NULL DEFAULT 0"
            )
        await self.database.commit()

    def _register_cleanup_task(self) -> None:
        """Register the recurring cache database cleanup task."""
        utc_hour, utc_minute = local_clock_time_to_utc(4, 0)
        desired_schedule = TaskSchedule.daily(hour=utc_hour, minute=utc_minute)
        self.mass.tasks.register_scheduled_task(
            task_id=CACHE_DATABASE_CLEANUP_TASK_ID,
            name="Cache database cleanup",
            handler=self.auto_cleanup,
            schedule=desired_schedule,
            translation_key="background_task.cache_database_cleanup",
            metadata={"task_domain": "cache_database_cleanup"},
            allow_retry=True,
        )
