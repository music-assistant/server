#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""provides a simple stateless caching system."""

import os
import functools
import datetime
import time
from functools import reduce
import aiosqlite

from .utils import run_periodic, LOGGER


class Cache(object):
    """basic stateless caching system."""

    _db = None

    def __init__(self, mass):
        """Initialize our caching class."""
        self.mass = mass
        if not os.path.isdir(mass.datapath):
            raise FileNotFoundError(
                f"data directory {mass.datapath} does not exist!")
        self._dbfile = os.path.join(mass.datapath, "cache.db")

    async def setup(self):
        """Async initialize of cache module."""
        self._db = await aiosqlite.connect(self._dbfile, timeout=30)
        self._db.row_factory = aiosqlite.Row
        await self.mass.add_event_listener(self.on_shutdown, "shutdown")
        await self._db.execute("""CREATE TABLE IF NOT EXISTS simplecache(
            id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)""")
        await self._db.commit()
        self.mass.event_loop.create_task(self.auto_cleanup())

    async def on_shutdown(self, msg, msg_details):
        """Handle shutdown event, close db connection."""
        await self._db.close()
        LOGGER.info("cache db connection closed")

    async def get(self, endpoint, checksum=""):
        """
            get object from cache and return the results
            endpoint: the (unique) name of the cache object as reference
            checkum: optional argument to check if the checksum in the
                     cacheobject matches the checkum provided
        """
        result = None
        cur_time = int(time.time())
        checksum = self._get_checksum(checksum)
        sql_query = "SELECT expires, data, checksum FROM simplecache WHERE id = ?"
        async with self._db.execute(sql_query, (endpoint, )) as cursor:
            cache_data = await cursor.fetchone()
            if not cache_data:
                LOGGER.debug('no cache data for %s', endpoint)
            elif cache_data['expires'] < cur_time:
                LOGGER.debug('cache expired for %s', endpoint)
            elif checksum and cache_data['checksum'] != checksum:
                LOGGER.debug('cache checksum mismatch for %s', endpoint)
            if cache_data and cache_data['expires'] > cur_time:
                if checksum is None or cache_data['checksum'] == checksum:
                    LOGGER.debug('return cache data for %s', endpoint)
                    result = eval(cache_data[1])
        return result

    async def set(self,
                  endpoint,
                  data,
                  checksum="",
                  expiration=datetime.timedelta(days=14)):
        """
            set data in cache
        """
        checksum = self._get_checksum(checksum)
        expires = int(time.time() + expiration.seconds)
        data = repr(data)
        sql_query = """INSERT OR REPLACE INTO simplecache
            (id, expires, data, checksum) VALUES (?, ?, ?, ?)"""
        await self._db.execute(sql_query, (endpoint, expires, data, checksum))
        await self._db.commit()

    @run_periodic(3600)
    async def auto_cleanup(self):
        """ (scheduled) auto cleanup task """
        cur_timestamp = int(time.time())
        LOGGER.debug("Running cleanup...")
        sql_query = "SELECT id, expires FROM simplecache"
        async with self._db.execute(sql_query) as cursor:
            cache_objects = await cursor.fetchall()
        for cache_data in cache_objects:
            cache_id = cache_data['id']
            # clean up db cache object only if expired
            if cache_data['expires'] < cur_timestamp:
                sql_query = "DELETE FROM simplecache WHERE id = ?"
                await self._db.execute(sql_query, (cache_id, ))
                LOGGER.debug("delete from db %s", cache_id)
        # compact db
        await self._db.commit()
        await self._db.execute("VACUUM;")
        await self._db.commit()
        LOGGER.debug("Auto cleanup done")

    @staticmethod
    def _get_timestamp(date_time):
        """Converts a datetime object to unix timestamp"""
        return int(time.mktime(date_time.timetuple()))

    @staticmethod
    def _get_checksum(stringinput):
        """get int checksum from string"""
        if not stringinput:
            return 0
        else:
            stringinput = str(stringinput)
        return reduce(lambda x, y: x + y, map(ord, stringinput))


def use_cache(cache_days=14, cache_hours=8):
    def wrapper(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            if kwargs.get("ignore_cache"):
                return await func(*args, **kwargs)
            cache_checksum = kwargs.get("cache_checksum")
            method_class = args[0]
            method_class_name = method_class.__class__.__name__
            cache_str = "%s.%s" % (method_class_name, func.__name__)
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
                if key in ["ignore_cache", "cache_checksum"]:
                    continue
                value = kwargs[key]
                if isinstance(value, dict):
                    for subkey in sorted(list(value.keys())):
                        subvalue = value[subkey]
                        cache_str += ".%s%s" % (subkey, subvalue)
                else:
                    cache_str += ".%s%s" % (key, value)
            cache_str = cache_str.lower()
            cachedata = await method_class.cache.get(cache_str,
                                                     checksum=cache_checksum)
            if cachedata is not None:
                return cachedata
            else:
                result = await func(*args, **kwargs)
                await method_class.cache.set(
                    cache_str,
                    result,
                    checksum=cache_checksum,
                    expiration=datetime.timedelta(days=cache_days,
                                                  hours=cache_hours),
                )
                return result

        return wrapped

    return wrapper
