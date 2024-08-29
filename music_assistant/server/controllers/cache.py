"""Provides a simple stateless caching system."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, MutableMapping
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from music_assistant.common.helpers.json import json_dumps, json_loads
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.constants import DB_TABLE_CACHE, DB_TABLE_SETTINGS, MASS_LOGGER_NAME
from music_assistant.server.helpers.database import DatabaseConnection
from music_assistant.server.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import CoreConfig

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.cache")
CONF_CLEAR_CACHE = "clear_cache"
DB_SCHEMA_VERSION = 5


class CacheController(CoreController):
    """Basic cache controller using both memory and database."""

    domain: str = "cache"

    def __init__(self, *args, **kwargs) -> None:
        """Initialize core controller."""
        super().__init__(*args, **kwargs)
        self.database: DatabaseConnection | None = None
        self._mem_cache = MemoryCache(500)
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
        self.__schedule_cleanup_task()

    async def close(self) -> None:
        """Cleanup on exit."""
        await self.database.close()

    async def get(
        self,
        key: str,
        checksum: str | None = None,
        default=None,
        category: int = 0,
        base_key: str = "",
    ) -> Any:
        """Get object from cache and return the results.

        cache_key: the (unique) name of the cache object as reference
        checksum: optional argument to check if the checksum in the
                    cacheobject matches the checksum provided
        category: optional category to group cache objects
        base_key: optional base key to group cache objects
        """
        if not key:
            return None
        cur_time = int(time.time())
        if checksum is not None and not isinstance(checksum, str):
            checksum = str(checksum)

        # try memory cache first
        memory_key = f"{category}/{base_key}/{key}"
        cache_data = self._mem_cache.get(memory_key)
        if cache_data and (not checksum or cache_data[1] == checksum) and cache_data[2] >= cur_time:
            return cache_data[0]
        # fall back to db cache
        if (
            db_row := await self.database.get_row(
                DB_TABLE_CACHE, {"category": category, "base_key": base_key, "sub_key": key}
            )
        ) and (not checksum or db_row["checksum"] == checksum and db_row["expires"] >= cur_time):
            try:
                data = await asyncio.to_thread(json_loads, db_row["data"])
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.error(
                    "Error parsing cache data for %s: %s",
                    memory_key,
                    str(exc),
                    exc_info=exc if self.logger.isEnabledFor(10) else None,
                )
            else:
                # also store in memory cache for faster access
                self._mem_cache[memory_key] = (
                    data,
                    db_row["checksum"],
                    db_row["expires"],
                )
                return data
        return default

    async def set(
        self, key, data, checksum="", expiration=(86400 * 30), category: int = 0, base_key: str = ""
    ) -> None:
        """Set data in cache."""
        if not key:
            return
        if checksum is not None and not isinstance(checksum, str):
            checksum = str(checksum)
        expires = int(time.time() + expiration)
        memory_key = f"{category}/{base_key}/{key}"
        self._mem_cache[memory_key] = (data, checksum, expires)
        if (expires - time.time()) < 3600 * 4:
            # do not cache items in db with short expiration
            return
        data = await asyncio.to_thread(json_dumps, data)
        await self.database.insert_or_replace(
            DB_TABLE_CACHE,
            {
                "category": category,
                "base_key": base_key,
                "sub_key": key,
                "expires": expires,
                "checksum": checksum,
                "data": data,
            },
        )

    async def delete(
        self, key: str | None, category: int | None = None, base_key: str | None = None
    ) -> None:
        """Delete data from cache."""
        match: dict[str, str | int] = {}
        if key is not None:
            match["sub_key"] = key
        if category is not None:
            match["category"] = category
        if base_key is not None:
            match["base_key"] = base_key
        if key is not None and category is not None and base_key is not None:
            self._mem_cache.pop(f"{category}/{base_key}/{key}", None)
        else:
            self._mem_cache.clear()
        await self.database.delete(DB_TABLE_CACHE, match)

    async def clear(
        self,
        key_filter: str | None = None,
        category: int | None = None,
        base_key_filter: str | None = None,
    ) -> None:
        """Clear all/partial items from cache."""
        self._mem_cache.clear()
        self.logger.info("Clearing database...")
        query_parts: list[str] = []
        if category is not None:
            query_parts.append(f"category = {category}")
        if base_key_filter is not None:
            query_parts.append(f"base_key LIKE '%{base_key_filter}%'")
        if key_filter is not None:
            query_parts.append(f"sub_key LIKE '%{key_filter}%'")
        query = "WHERE " + " AND ".join(query_parts) if query_parts else None
        await self.database.delete(DB_TABLE_CACHE, query=query)
        await self.database.vacuum()
        self.logger.info("Clearing database DONE")

    async def auto_cleanup(self) -> None:
        """Run scheduled auto cleanup task."""
        self.logger.debug("Running automatic cleanup...")
        # simply reset the memory cache
        self._mem_cache.clear()
        cur_timestamp = int(time.time())
        cleaned_records = 0
        for db_row in await self.database.get_rows(DB_TABLE_CACHE):
            # clean up db cache object only if expired
            if db_row["expires"] < cur_timestamp:
                await self.delete(db_row["key"])
                cleaned_records += 1
            await asyncio.sleep(0)  # yield to eventloop
        if cleaned_records > 50:
            self.logger.debug("Compacting database...")
            await self.database.vacuum()
            self.logger.debug("Compacting database done")
        self.logger.debug("Automatic cleanup finished (cleaned up %s records)", cleaned_records)

    async def _setup_database(self) -> None:
        """Initialize database."""
        db_path = os.path.join(self.mass.storage_path, "cache.db")
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

            if prev_version < DB_SCHEMA_VERSION:
                # for now just keep it simple and just recreate the table(s)
                await self.database.execute(f"DROP TABLE IF EXISTS {DB_TABLE_CACHE}")

                # recreate missing table(s)
                await self.__create_database_tables()

        # store current schema version
        await self.database.insert_or_replace(
            DB_TABLE_SETTINGS,
            {"key": "version", "value": str(DB_SCHEMA_VERSION), "type": "str"},
        )
        await self.__create_database_indexes()
        # compact db
        self.logger.debug("Compacting database...")
        try:
            await self.database.vacuum()
        except Exception as err:
            self.logger.warning("Database vacuum failed: %s", str(err))
        else:
            self.logger.debug("Compacting database done")

    async def __create_database_tables(self) -> None:
        """Create database table(s)."""
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
                    [base_key] TEXT NOT NULL,
                    [sub_key] TEXT NOT NULL,
                    [expires] INTEGER NOT NULL,
                    [data] TEXT,
                    [checksum] TEXT NULL,
                    UNIQUE(category, base_key, sub_key)
                    )"""
        )

        await self.database.commit()

    async def __create_database_indexes(self) -> None:
        """Create database indexes."""
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_category_idx "
            f"ON {DB_TABLE_CACHE}(category);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_base_key_idx "
            f"ON {DB_TABLE_CACHE}(base_key);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_sub_key_idx "
            f"ON {DB_TABLE_CACHE}(sub_key);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_category_base_key_idx "
            f"ON {DB_TABLE_CACHE}(category,base_key);"
        )
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_category_base_key_sub_key_idx "
            f"ON {DB_TABLE_CACHE}(category,base_key,sub_key);"
        )
        await self.database.commit()

    def __schedule_cleanup_task(self) -> None:
        """Schedule the cleanup task."""
        self.mass.create_task(self.auto_cleanup())
        # reschedule self
        self.mass.loop.call_later(3600, self.__schedule_cleanup_task)


Param = ParamSpec("Param")
RetType = TypeVar("RetType")


def use_cache(
    expiration: int = 86400 * 30,
    category: int = 0,
) -> Callable[[Callable[Param, RetType]], Callable[Param, RetType]]:
    """Return decorator that can be used to cache a method's result."""

    def wrapper(func: Callable[Param, RetType]) -> Callable[Param, RetType]:
        @functools.wraps(func)
        async def wrapped(*args: Param.args, **kwargs: Param.kwargs):
            method_class = args[0]
            method_class_name = method_class.__class__.__name__
            cache_base_key = f"{method_class_name}.{func.__name__}"
            cache_sub_key_parts = []
            skip_cache = kwargs.pop("skip_cache", False)
            cache_checksum = kwargs.pop("cache_checksum", "")
            if len(args) > 1:
                cache_sub_key_parts += args[1:]
            for key in sorted(kwargs.keys()):
                cache_sub_key_parts.append(f"{key}{kwargs[key]}")
            cache_sub_key = ".".join(cache_sub_key_parts)

            cachedata = await method_class.cache.get(
                cache_sub_key, checksum=cache_checksum, category=category, base_key=cache_base_key
            )

            if not skip_cache and cachedata is not None:
                return cachedata
            result = await func(*args, **kwargs)
            asyncio.create_task(
                method_class.cache.set(
                    cache_sub_key,
                    result,
                    expiration=expiration,
                    checksum=cache_checksum,
                    category=category,
                    base_key=cache_base_key,
                )
            )
            return result

        return wrapped

    return wrapper


class MemoryCache(MutableMapping):
    """Simple limited in-memory cache implementation."""

    def __init__(self, maxlen: int) -> None:
        """Initialize."""
        self._maxlen = maxlen
        self.d = OrderedDict()

    @property
    def maxlen(self) -> int:
        """Return max length."""
        return self._maxlen

    def get(self, key: str, default: Any = None) -> Any:
        """Return item or default."""
        return self.d.get(key, default)

    def pop(self, key: str, default: Any = None) -> Any:
        """Pop item from collection."""
        return self.d.pop(key, default)

    def __getitem__(self, key: str) -> Any:
        """Get item."""
        self.d.move_to_end(key)
        return self.d[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """Set item."""
        if key in self.d:
            self.d.move_to_end(key)
        elif len(self.d) == self.maxlen:
            self.d.popitem(last=False)
        self.d[key] = value

    def __delitem__(self, key) -> None:
        """Delete item."""
        del self.d[key]

    def __iter__(self) -> Iterator:
        """Iterate items."""
        return self.d.__iter__()

    def __len__(self) -> int:
        """Return length."""
        return len(self.d)

    def clear(self) -> None:
        """Clear cache."""
        self.d.clear()
