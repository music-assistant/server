"""Provides a simple stateless caching system."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from typing import TYPE_CHECKING, Any

from music_assistant.common.helpers.json import json_dumps, json_loads
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.constants import (
    DB_SCHEMA_VERSION,
    DB_TABLE_CACHE,
    DB_TABLE_SETTINGS,
    ROOT_LOGGER_NAME,
)
from music_assistant.server.helpers.database import DatabaseConnection
from music_assistant.server.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import CoreConfig

LOGGER = logging.getLogger(f"{ROOT_LOGGER_NAME}.cache")
CONF_CLEAR_CACHE = "clear_cache"


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

    async def get(self, cache_key: str, checksum: str | None = None, default=None):
        """Get object from cache and return the results.

        cache_key: the (unique) name of the cache object as reference
        checksum: optional argument to check if the checksum in the
                    cacheobject matches the checksum provided
        """
        if not cache_key:
            return None
        cur_time = int(time.time())
        if checksum is not None and not isinstance(checksum, str):
            checksum = str(checksum)

        # try memory cache first
        cache_data = self._mem_cache.get(cache_key)
        if cache_data and (not checksum or cache_data[1] == checksum) and cache_data[2] >= cur_time:
            return cache_data[0]
        # fall back to db cache
        if (db_row := await self.database.get_row(DB_TABLE_CACHE, {"key": cache_key})) and (
            not checksum or db_row["checksum"] == checksum and db_row["expires"] >= cur_time
        ):
            try:
                data = await asyncio.to_thread(json_loads, db_row["data"])
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.error(
                    "Error parsing cache data for %s: %s",
                    cache_key,
                    str(exc),
                    exc_info=exc if self.logger.isEnabledFor(10) else None,
                )
            else:
                # also store in memory cache for faster access
                self._mem_cache[cache_key] = (
                    data,
                    db_row["checksum"],
                    db_row["expires"],
                )
                return data
        return default

    async def set(self, cache_key, data, checksum="", expiration=(86400 * 30)) -> None:
        """Set data in cache."""
        if not cache_key:
            return
        if checksum is not None and not isinstance(checksum, str):
            checksum = str(checksum)
        expires = int(time.time() + expiration)
        self._mem_cache[cache_key] = (data, checksum, expires)
        if (expires - time.time()) < 3600 * 4:
            # do not cache items in db with short expiration
            return
        data = await asyncio.to_thread(json_dumps, data)
        await self.database.insert(
            DB_TABLE_CACHE,
            {"key": cache_key, "expires": expires, "checksum": checksum, "data": data},
            allow_replace=True,
        )

    async def delete(self, cache_key) -> None:
        """Delete data from cache."""
        self._mem_cache.pop(cache_key, None)
        await self.database.delete(DB_TABLE_CACHE, {"key": cache_key})

    async def clear(self, key_filter: str | None = None) -> None:
        """Clear all/partial items from cache."""
        self._mem_cache = {}
        self.logger.info("Clearing database...")
        query = f"key LIKE '%{key_filter}%'" if key_filter else None
        await self.database.delete(DB_TABLE_CACHE, query=query)
        await self.database.vacuum()
        self.logger.info("Clearing database DONE")

    async def auto_cleanup(self) -> None:
        """Sceduled auto cleanup task."""
        self.logger.debug("Running automatic cleanup...")
        # for now we simply reset the memory cache
        self._mem_cache = {}
        cur_timestamp = int(time.time())
        cleaned_records = 0
        for db_row in await self.database.get_rows(DB_TABLE_CACHE):
            # clean up db cache object only if expired
            if db_row["expires"] < cur_timestamp:
                await self.delete(db_row["key"])
                cleaned_records += 1
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
                    key TEXT UNIQUE NOT NULL, expires INTEGER NOT NULL,
                    data TEXT, checksum TEXT NULL)"""
        )

        # create indexes
        await self.database.execute(
            f"CREATE INDEX IF NOT EXISTS {DB_TABLE_CACHE}_key_idx on {DB_TABLE_CACHE}(key);"
        )

    def __schedule_cleanup_task(self) -> None:
        """Schedule the cleanup task."""
        self.mass.create_task(self.auto_cleanup())
        # reschedule self
        self.mass.loop.call_later(3600, self.__schedule_cleanup_task)


def use_cache(expiration=86400 * 30):
    """Return decorator that can be used to cache a method's result."""

    def wrapper(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            method_class = args[0]
            method_class_name = method_class.__class__.__name__
            cache_key_parts = [method_class_name, func.__name__]
            skip_cache = kwargs.pop("skip_cache", False)
            cache_checksum = kwargs.pop("cache_checksum", "")
            if len(args) > 1:
                cache_key_parts += args[1:]
            for key in sorted(kwargs.keys()):
                cache_key_parts.append(f"{key}{kwargs[key]}")
            cache_key = ".".join(cache_key_parts)

            cachedata = await method_class.cache.get(cache_key, checksum=cache_checksum)

            if not skip_cache and cachedata is not None:
                return cachedata
            result = await func(*args, **kwargs)
            asyncio.create_task(
                method_class.cache.set(
                    cache_key, result, expiration=expiration, checksum=cache_checksum
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
