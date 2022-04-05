"""Provides a simple stateless caching system."""
from __future__ import annotations

import asyncio
import functools
import pickle
import time
from typing import Awaitable

from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task

DB_TABLE = "cache"


class Cache:
    """Basic cache using both memory and database."""

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize our caching class."""
        self.mass = mass
        self.logger = mass.logger.getChild("cache")
        self._mem_cache = {}

    async def setup(self) -> None:
        """Async initialize of cache module."""
        # prepare database
        async with self.mass.database.get_db() as _db:
            await _db.execute(
                f"""CREATE TABLE IF NOT EXISTS {DB_TABLE}(
                    key TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)"""
            )
        self.__schedule_cleanup_task()

    async def get(self, cache_key, checksum="", default=None):
        """
        Get object from cache and return the results.

        cache_key: the (unique) name of the cache object as reference
        checkum: optional argument to check if the checksum in the
                    cacheobject matches the checkum provided
        """
        cur_time = int(time.time())
        checksum = self._get_checksum(checksum)

        # try memory cache first
        cache_data = self._mem_cache.get(cache_key)
        if (
            cache_data
            and (not checksum or cache_data[1] == checksum)
            and cache_data[2] >= cur_time
        ):
            return cache_data[0]
        # fall back to db cache
        if db_row := await self.mass.database.get_row(DB_TABLE, {"key": cache_key}):
            if (
                not checksum
                or db_row["checksum"] == checksum
                and db_row["expires"] >= cur_time
            ):
                try:
                    data = await asyncio.get_running_loop().run_in_executor(
                        None, pickle.loads, db_row["data"]
                    )
                except Exception:  # pylint: disable=broad-except
                    self.logger.warning("Error parsing cache data for %s", cache_key)
                else:
                    # also store in memory cache for faster access
                    if cache_key not in self._mem_cache:
                        self._mem_cache[cache_key] = (
                            data,
                            db_row["checksum"],
                            db_row["expires"],
                        )
                    return data
        self.logger.debug("no cache data for %s", cache_key)
        return default

    async def set(self, cache_key, data, checksum="", expiration=(86400 * 30)):
        """Set data in cache."""
        checksum = self._get_checksum(checksum)
        expires = int(time.time() + expiration)
        self._mem_cache[cache_key] = (data, checksum, expires)
        data = await asyncio.get_running_loop().run_in_executor(
            None, pickle.dumps, data
        )
        await self.mass.database.insert_or_replace(
            DB_TABLE,
            {"key": cache_key, "expires": expires, "checksum": checksum, "data": data},
        )

    async def delete(self, cache_key):
        """Delete data from cache."""
        self._mem_cache.pop(cache_key, None)
        await self.mass.database.delete(DB_TABLE, {"key": cache_key})

    async def auto_cleanup(self):
        """Sceduled auto cleanup task."""
        # for now we simply reset the memory cache
        self._mem_cache = {}
        cur_timestamp = int(time.time())
        for db_row in await self.mass.database.get_rows(DB_TABLE):
            # clean up db cache object only if expired
            if db_row["expires"] < cur_timestamp:
                await self.delete(db_row["key"])
        # compact db
        async with self.mass.database.get_db() as _db:
            await _db.execute("VACUUM")

    def __schedule_cleanup_task(self):
        """Schedule the cleanup task."""
        self.mass.add_job(self.auto_cleanup(), "Cleanup cache")
        # reschedule self
        self.mass.loop.call_later(3600, self.__schedule_cleanup_task)

    @staticmethod
    def _get_checksum(stringinput):
        """Get int checksum from string."""
        if not stringinput:
            return 0
        stringinput = str(stringinput)
        return functools.reduce(lambda x, y: x + y, map(ord, stringinput))


async def cached(
    cache,
    cache_key: str,
    coro_func: Awaitable,
    *args,
    expires: int = (86400 * 30),
    checksum=None,
):
    """Return helper method to store results of a coroutine in the cache."""
    cache_result = await cache.get(cache_key, checksum)
    if cache_result is not None:
        return cache_result
    if asyncio.iscoroutine(coro_func):
        result = await coro_func
    else:
        result = await coro_func(*args)
    create_task(cache.set(cache_key, result, checksum, expires))
    return result
