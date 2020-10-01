"""Provides a simple stateless caching system."""

import functools
import logging
import os
import pickle
import time
from functools import reduce

import aiosqlite
from music_assistant.helpers.util import run_periodic

LOGGER = logging.getLogger("mass")


class Cache:
    """Basic stateless caching system."""

    _db = None

    def __init__(self, mass):
        """Initialize our caching class."""
        self.mass = mass
        self._dbfile = os.path.join(mass.config.data_path, "cache.db")

    async def async_setup(self):
        """Async initialize of cache module."""
        async with aiosqlite.connect(self._dbfile, timeout=180) as db_conn:
            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS simplecache(
                id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)"""
            )
            await db_conn.commit()
            await db_conn.execute("VACUUM;")
            await db_conn.commit()
        self.mass.add_job(self.async_auto_cleanup())

    async def async_get(self, cache_key, checksum=""):
        """
        Get object from cache and return the results.

        cache_key: the (unique) name of the cache object as reference
        checkum: optional argument to check if the checksum in the
                    cacheobject matches the checkum provided
        """
        result = None
        cur_time = int(time.time())
        checksum = self._get_checksum(checksum)
        sql_query = "SELECT expires, data, checksum FROM simplecache WHERE id = ?"
        async with aiosqlite.connect(self._dbfile, timeout=180) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            async with db_conn.execute(sql_query, (cache_key,)) as cursor:
                cache_data = await cursor.fetchone()
                if not cache_data:
                    LOGGER.debug("no cache data for %s", cache_key)
                elif cache_data["expires"] < cur_time:
                    LOGGER.debug("cache expired for %s", cache_key)
                elif checksum and cache_data["checksum"] != checksum:
                    LOGGER.debug("cache checksum mismatch for %s", cache_key)
                if cache_data and cache_data["expires"] > cur_time:
                    if checksum is None or cache_data["checksum"] == checksum:
                        LOGGER.debug("return cache data for %s", cache_key)
                        result = pickle.loads(cache_data[1])
        return result

    async def async_set(self, cache_key, data, checksum="", expiration=(86400 * 30)):
        """Set data in cache."""
        checksum = self._get_checksum(checksum)
        expires = int(time.time() + expiration)
        data = pickle.dumps(data)
        sql_query = """INSERT OR REPLACE INTO simplecache
            (id, expires, data, checksum) VALUES (?, ?, ?, ?)"""
        async with aiosqlite.connect(self._dbfile, timeout=180) as db_conn:
            await db_conn.execute(sql_query, (cache_key, expires, data, checksum))
            await db_conn.commit()

    @run_periodic(3600)
    async def async_auto_cleanup(self):
        """Sceduled auto cleanup task."""
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
                    LOGGER.debug("delete from db %s", cache_id)
            # compact db
            await db_conn.commit()
        LOGGER.debug("Auto cleanup done")

    @staticmethod
    def _get_checksum(stringinput):
        """Get int checksum from string."""
        if not stringinput:
            return 0
        stringinput = str(stringinput)
        return reduce(lambda x, y: x + y, map(ord, stringinput))


async def async_cached_generator(
    cache, cache_key, coro_func, expires=(86400 * 30), checksum=None
):
    """Return helper method to store results of a async generator in the cache."""
    cache_result = await cache.async_get(cache_key, checksum)
    if cache_result is not None:
        for item in cache_result:
            yield item
    else:
        # nothing in cache, yield from generator and store in cache when complete
        cache_result = []
        async for item in coro_func:
            yield item
            cache_result.append(item)
        # store results in cache
        await cache.async_set(cache_key, cache_result, checksum, expires)


async def async_cached(
    cache, cache_key, coro_func, expires=(86400 * 30), checksum=None
):
    """Return helper method to store results of a coroutine in the cache."""
    cache_result = await cache.async_get(cache_key, checksum)
    # normal async function
    if cache_result is not None:
        return cache_result
    result = await coro_func
    await cache.async_set(cache_key, cache_result, checksum, expires)
    return result


def async_use_cache(cache_days=14, cache_checksum=None):
    """Return decorator that can be used to cache a method's result."""

    def wrapper(func):
        @functools.wraps(func)
        async def async_wrapped(*args, **kwargs):
            method_class = args[0]
            method_class_name = method_class.__class__.__name__
            cache_str = "%s.%s" % (method_class_name, func.__name__)
            cache_str += __cache_id_from_args(*args, **kwargs)
            cache_str = cache_str.lower()
            cachedata = await method_class.cache.async_get(cache_str)
            if cachedata is not None:
                return cachedata
            result = await func(*args, **kwargs)
            await method_class.cache.async_set(
                cache_str,
                result,
                checksum=cache_checksum,
                expiration=(86400 * cache_days),
            )
            return result

        return async_wrapped

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
