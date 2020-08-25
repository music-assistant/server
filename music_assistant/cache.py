#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""provides a simple stateless caching system."""

import functools
from functools import reduce
import os
import pickle
import time

import aiosqlite
from music_assistant.utils import LOGGER, run_periodic


class Cache(object):
    """basic stateless caching system."""

    _db = None

    def __init__(self, mass):
        """Initialize our caching class."""
        self.mass = mass
        if not os.path.isdir(mass.datapath):
            raise FileNotFoundError(f"data directory {mass.datapath} does not exist!")
        self._dbfile = os.path.join(mass.datapath, "cache.db")

    async def setup(self):
        """Async initialize of cache module."""
        self._db = await aiosqlite.connect(self._dbfile, timeout=30)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS simplecache(
            id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)"""
        )
        await self._db.commit()
        self.mass.loop.create_task(self.auto_cleanup())

    async def close(self):
        """Handle shutdown event, close db connection."""
        await self._db.close()
        LOGGER.info("cache db connection closed")

    async def get(self, cache_key, checksum=""):
        """
            get object from cache and return the results
            cache_key: the (unique) name of the cache object as reference
            checkum: optional argument to check if the checksum in the
                     cacheobject matches the checkum provided
        """
        result = None
        cur_time = int(time.time())
        checksum = self._get_checksum(checksum)
        sql_query = "SELECT expires, data, checksum FROM simplecache WHERE id = ?"
        async with self._db.execute(sql_query, (cache_key,)) as cursor:
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

    async def set(self, cache_key, data, checksum="", expiration=(86400 * 30)):
        """
            set data in cache
        """
        checksum = self._get_checksum(checksum)
        expires = int(time.time() + expiration)
        data = pickle.dumps(data)
        sql_query ="""INSERT OR REPLACE INTO simplecache
            (id, expires, data, checksum) VALUES (?, ?, ?, ?)"""
        await self._db.execute(sql_query, (cache_key, expires, data, checksum))
        await self._db.commit()

    @run_periodic(3600)
    async def auto_cleanup(self):
        """(scheduled) auto cleanup task"""
        cur_timestamp = int(time.time())
        LOGGER.debug("Running cleanup...")
        sql_query = "SELECT id, expires FROM simplecache"
        async with self._db.execute(sql_query) as cursor:
            cache_objects = await cursor.fetchall()
        for cache_data in cache_objects:
            cache_id = cache_data["id"]
            # clean up db cache object only if expired
            if cache_data["expires"] < cur_timestamp:
                sql_query = "DELETE FROM simplecache WHERE id = ?"
                await self._db.execute(sql_query, (cache_id,))
                LOGGER.debug("delete from db %s", cache_id)
        # compact db
        await self._db.commit()
        await self._db.execute("VACUUM;")
        await self._db.commit()
        LOGGER.debug("Auto cleanup done")

    @staticmethod
    def _get_checksum(stringinput):
        """get int checksum from string"""
        if not stringinput:
            return 0
        else:
            stringinput = str(stringinput)
        return reduce(lambda x, y: x + y, map(ord, stringinput))


async def cached_iterator(
    cache, iter_func, cache_key, expires=(86400 * 30), checksum=None
):
    """Helper method to store results of an iterator in the cache."""
    cache_result = await cache.get(cache_key, checksum)
    if cache_result:
        for item in cache_result:
            yield item
    else:
        # nothing in cache, yield from iterator and store in cache when complete
        cache_result = []
        async for item in iter_func:
            yield item
            cache_result.append(item)
        await cache.set(cache_key, cache_result, checksum, expires)


async def cached(cache, cache_key, coro_func, *args, **kwargs):
    """Helper method to store results of a coroutine in the cache."""
    cache_result = await cache.get(cache_key)
    if cache_result is not None:
        return cache_result
    result = await coro_func(*args, **kwargs)
    await cache.set(cache_key, result)
    return result


def use_cache(cache_days=14, cache_checksum=None):
    """decorator that can be used to cache a method's result."""

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
            else:
                result = await func(*args, **kwargs)
                await method_class.cache.set(
                    cache_str,
                    result,
                    checksum=cache_checksum,
                    expiration=(86400 * cache_days),
                )
                return result

        return wrapped

    return wrapper


def __cache_id_from_args(*args, **kwargs):
    """parse arguments to build cache id"""
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
