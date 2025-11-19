"""Provides a simple stateless caching system."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterator, MutableMapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast, get_type_hints

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import DB_TABLE_CACHE, DB_TABLE_SETTINGS, MASS_LOGGER_NAME
from music_assistant.helpers.api import parse_value
from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.json import async_json_loads, json_dumps
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant import MusicAssistant
    from music_assistant.models.provider import Provider

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.cache")
CONF_CLEAR_CACHE = "clear_cache"
DEFAULT_CACHE_EXPIRATION = 86400 * 30  # 30 days
DB_SCHEMA_VERSION = 6

BYPASS_CACHE: ContextVar[bool] = ContextVar("BYPASS_CACHE", default=False)


class CacheController(CoreController):
    """Basic cache controller using both memory and database."""

    domain: str = "cache"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize core controller."""
        super().__init__(mass)
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
        if self.database:
            await self.database.close()

    async def get(
        self,
        key: str,
        provider: str = "default",
        category: int = 0,
        checksum: str | int | None = None,
        default: Any = None,
        allow_bypass: bool = True,
    ) -> Any:
        """Get object from cache and return the results.

        - key: the (unique) lookup key of the cache object as reference
        - provider: optional provider id to group cache objects
        - category: optional category to group cache objects
        - checksum: optional argument to check if the checksum in the
                    cache object matches the checksum provided
        - default: value to return if no cache object is found
        """
        assert self.database is not None
        assert key, "No key provided"
        if allow_bypass and BYPASS_CACHE.get():
            return default
        cur_time = int(time.time())
        if checksum is not None and not isinstance(checksum, str):
            checksum = str(checksum)
        # try memory cache first
        memory_key = f"{provider}/{category}/{key}"
        cache_data = self._mem_cache.get(memory_key)
        if cache_data and (not checksum or cache_data[1] == checksum) and cache_data[2] >= cur_time:
            return cache_data[0]
        # fall back to db cache
        if (
            db_row := await self.database.get_row(
                DB_TABLE_CACHE, {"category": category, "provider": provider, "key": key}
            )
        ) and (not checksum or (db_row["checksum"] == checksum and db_row["expires"] >= cur_time)):
            try:
                data = await async_json_loads(db_row["data"])
            except Exception as exc:
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
        self,
        key: str,
        data: Any,
        expiration: int = DEFAULT_CACHE_EXPIRATION,
        provider: str = "default",
        category: int = 0,
        checksum: str | None = None,
        persistent: bool = False,
    ) -> None:
        """
        Set data in cache.

        - key: the (unique) lookup key of the cache object as reference
        - data: the actual data to store in the cache
        - expiration: time in seconds the cache object should be valid
        - provider: optional provider id to group cache objects
        - category: optional category to group cache objects
        - checksum: optional argument to store with the cache object
        - persistent: if True the cache object will not be deleted when clearing the cache
        """
        assert self.database is not None
        if not key:
            return
        if checksum is not None:
            checksum = str(checksum)
        expires = int(time.time() + expiration)
        memory_key = f"{provider}/{category}/{key}"
        self._mem_cache[memory_key] = (data, checksum, expires)
        if (expires - time.time()) < 1800:
            # do not cache items in db with short expiration
            return
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
        if key is not None and category is not None and provider is not None:
            self._mem_cache.pop(f"{provider}/{category}/{key}", None)
        else:
            self._mem_cache.clear()
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
        self._mem_cache.clear()
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
        # simply reset the memory cache
        self._mem_cache.clear()
        cur_timestamp = int(time.time())
        cleaned_records = 0
        for db_row in await self.database.get_rows(DB_TABLE_CACHE):
            # clean up db cache object only if expired
            if db_row["expires"] < cur_timestamp:
                await self.database.delete(DB_TABLE_CACHE, {"id": db_row["id"]})
                cleaned_records += 1
            await asyncio.sleep(0)  # yield to eventloop
        self.logger.debug("Automatic cleanup finished (cleaned up %s records)", cleaned_records)

    @asynccontextmanager
    async def handle_refresh(self, bypass: bool) -> AsyncGenerator[None, None]:
        """Handle the cache bypass."""
        try:
            token = BYPASS_CACHE.set(bypass)
            yield None
        finally:
            BYPASS_CACHE.reset(token)

    async def _setup_database(self) -> None:
        """Initialize database."""
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

    def __schedule_cleanup_task(self) -> None:
        """Schedule the cleanup task."""
        self.mass.create_task(self.auto_cleanup())
        # reschedule self
        self.mass.loop.call_later(3600, self.__schedule_cleanup_task)


Param = ParamSpec("Param")
RetType = TypeVar("RetType")


ProviderT = TypeVar("ProviderT", bound="Provider | CoreController")
P = ParamSpec("P")
R = TypeVar("R")


def use_cache(
    expiration: int = DEFAULT_CACHE_EXPIRATION,
    category: int = 0,
    persistent: bool = False,
    cache_checksum: str | None = None,
    allow_bypass: bool = True,
) -> Callable[
    [Callable[Concatenate[ProviderT, P], Awaitable[R]]],
    Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]],
]:
    """Return decorator that can be used to cache a method's result."""

    def _decorator(
        func: Callable[Concatenate[ProviderT, P], Awaitable[R]],
    ) -> Callable[Concatenate[ProviderT, P], Coroutine[Any, Any, R]]:
        @functools.wraps(func)
        async def wrapper(self: ProviderT, *args: P.args, **kwargs: P.kwargs) -> R:
            cache = self.mass.cache
            provider_id = getattr(self, "instance_id", self.domain)

            # create a cache key dynamically based on the (remaining) args/kwargs
            cache_key_parts = [func.__name__, *args]
            for key in sorted(kwargs.keys()):
                cache_key_parts.append(f"{key}{kwargs[key]}")
            cache_key = ".".join(map(str, cache_key_parts))
            # try to retrieve data from the cache
            cachedata = await cache.get(
                cache_key,
                provider=provider_id,
                checksum=cache_checksum,
                category=category,
                allow_bypass=allow_bypass,
            )
            if cachedata is not None:
                type_hints = get_type_hints(func)
                return cast("R", parse_value(func.__name__, cachedata, type_hints["return"]))
            # get data from method/provider
            result = await func(self, *args, **kwargs)
            # store result in cache (but don't await)
            self.mass.create_task(
                cache.set(
                    key=cache_key,
                    data=result,
                    expiration=expiration,
                    provider=provider_id,
                    category=category,
                    checksum=cache_checksum,
                    persistent=persistent,
                )
            )
            return result

        return wrapper

    return _decorator


class MemoryCache(MutableMapping[str, Any]):
    """Simple limited in-memory cache implementation."""

    def __init__(self, maxlen: int) -> None:
        """Initialize."""
        self._maxlen = maxlen
        self.d: OrderedDict[str, Any] = OrderedDict()

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

    def __delitem__(self, key: str) -> None:
        """Delete item."""
        del self.d[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate items."""
        return self.d.__iter__()

    def __len__(self) -> int:
        """Return length."""
        return len(self.d)

    def clear(self) -> None:
        """Clear cache."""
        self.d.clear()
