"""Provides a simple stateless caching system."""

import asyncio
import functools
import logging
import os
import pickle
import time
from typing import Awaitable

import aiosqlite
from music_assistant.helpers.util import run_periodic

LOGGER = logging.getLogger("cache")


class Cache:
    """Basic stateless caching system."""

    _db = None

    def __init__(self, mass):
        """Initialize our caching class."""
        self.mass = mass
        self._dbfile = os.path.join(mass.config.data_path, ".cache.db")
        self._mem_cache = {}

    async def setup(self):
        """Async initialize of cache module."""
        async with aiosqlite.connect(self._dbfile, timeout=180) as db_conn:
            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS simplecache(
                id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)"""
            )
            await db_conn.commit()
            await db_conn.execute("VACUUM;")
            await db_conn.commit()
        self.mass.add_job(self.auto_cleanup())

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
        sql_query = "SELECT data, checksum, expires FROM simplecache WHERE id = ?"
        async with aiosqlite.connect(self._dbfile, timeout=180) as db_conn:
            async with db_conn.execute(sql_query, (cache_key,)) as cursor:
                cache_data = await cursor.fetchone()
                if (
                    cache_data
                    and (not checksum or cache_data[1] == checksum)
                    and cache_data[2] >= cur_time
                ):
                    data = await asyncio.get_running_loop().run_in_executor(
                        None, pickle.loads, cache_data[0]
                    )
                    # also store in memory cache for faster access
                    if cache_key not in self._mem_cache:
                        self._mem_cache[cache_key] = (
                            data,
                            cache_data[1],
                            cache_data[2],
                        )
                    return data
        LOGGER.debug("no cache data for %s", cache_key)
        return default

    async def set(self, cache_key, data, checksum="", expiration=(86400 * 30)):
        """Set data in cache."""
        checksum = self._get_checksum(checksum)
        expires = int(time.time() + expiration)
        self._mem_cache[cache_key] = (data, checksum, expires)
        data = await asyncio.get_running_loop().run_in_executor(
            None, pickle.dumps, data
        )
        sql_query = """INSERT OR REPLACE INTO simplecache
            (id, expires, data, checksum) VALUES (?, ?, ?, ?)"""
        async with aiosqlite.connect(self._dbfile, timeout=180) as db_conn:
            await db_conn.execute(sql_query, (cache_key, expires, data, checksum))
            await db_conn.commit()

    async def delete(self, cache_key):
        """Delete data from cache."""
        self._mem_cache.pop(cache_key, None)
        sql_query = "DELETE FROM simplecache WHERE id = ?"
        async with aiosqlite.connect(self._dbfile, timeout=180) as db_conn:
            await db_conn.execute(sql_query, (cache_key,))
            await db_conn.commit()

    @run_periodic(3600)
    async def auto_cleanup(self):
        """Sceduled auto cleanup task."""
        # for now we simply rest the memory cache
        self._mem_cache = {}
        cur_timestamp = int(time.time())
        LOGGER.debug("Running cleanup...")
        sql_query = "SELECT id, expires FROM simplecache"
        async with aiosqlite.connect(self._dbfile, timeout=600) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            async with db_conn.execute(sql_query) as cursor:
                cache_objects = await cursor.fetchall()
            for cache_data in cache_objects:
                cache_id = cache_data["id"]
                # clean up db cache object only if expired
                if cache_data["expires"] < cur_timestamp:
                    sql_query = "DELETE FROM simplecache WHERE id = ?"
                    await db_conn.execute(sql_query, (cache_id,))
            # compact db
            await db_conn.commit()
        LOGGER.debug("Auto cleanup done")

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
    checksum=None
):
    """Return helper method to store results of a coroutine in the cache."""
    cache_result = await cache.get(cache_key, checksum)
    if cache_result is not None:
        return cache_result
    result = await coro_func(*args)
    asyncio.create_task(cache.set(cache_key, result, checksum, expires))
    return result


def use_cache(cache_days=14, cache_checksum=None):
    """Return decorator that can be used to cache a method's result."""

    def wrapper(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            method_class = args[0]
            method_class_name = method_class.__class__.__name__
            cache_str = "%s.%s" % (method_class_name, func.__name__)
            cache_str += __cache_id_from_args(*args, **kwargs)
            cache_str = cache_str.lower()
            cachedata = await method_class.cache.get(cache_str)
            if cachedata is not None:
                return cachedata
            result = await func(*args, **kwargs)
            asyncio.create_task(
                method_class.cache.set(
                    cache_str,
                    result,
                    checksum=cache_checksum,
                    expiration=(86400 * cache_days),
                )
            )
            return result

        return wrapped

    return wrapper


def __cache_id_from_args(*args, **kwargs):
    """Parse arguments to build cache id."""
    cache_str = ""
    # append args to cache identifier
    for item in args[1:]:
        if isinstance(item, dict):
            for subkey in sorted(list(item.keys())):
                subvalue = item[subkey]
                cache_str += ".%s%s" % (subkey, subvalue)
        else:
            cache_str += ".%s" % item
    # append kwargs to cache identifier
    for key in sorted(list(kwargs.keys())):
        value = kwargs[key]
        if isinstance(value, dict):
            for subkey in sorted(list(value.keys())):
                subvalue = value[subkey]
                cache_str += ".%s%s" % (subkey, subvalue)
        else:
            cache_str += ".%s%s" % (key, value)
    return cache_str
