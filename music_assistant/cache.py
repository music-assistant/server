#!/usr/bin/python3
# -*- coding: utf-8 -*-

'''provides a simple stateless caching system'''

import datetime
import time
import sqlite3
from functools import reduce
import os
from utils import run_periodic, LOGGER, parse_track_title
import functools
import asyncio


class Cache(object):
    '''basic stateless caching system '''
    _exit = False
    _mem_cache = {}
    _busy_tasks = []
    _database = None

    def __init__(self):
        '''Initialize our caching class'''
        asyncio.ensure_future(self._do_cleanup())
        LOGGER.debug("Initialized")

    async def get(self, endpoint, checksum=""):
        '''
            get object from cache and return the results
            endpoint: the (unique) name of the cache object as reference
            checkum: optional argument to check if the checksum in the cacheobject matches the checkum provided
        '''
        checksum = self._get_checksum(checksum)
        cur_time = self._get_timestamp(datetime.datetime.now())
        result = None
        # 1: try memory cache first
        result = await self._get_mem_cache(endpoint, checksum, cur_time)
        # 2: fallback to _database cache
        if result is None:
            result = await self._get_db_cache(endpoint, checksum, cur_time)
        return result

    async def set(self, endpoint, data, checksum="", expiration=datetime.timedelta(days=14)):
        '''
            set data in cache
        '''
        task_name = "set.%s" % endpoint
        self._busy_tasks.append(task_name)
        checksum = self._get_checksum(checksum)
        expires = self._get_timestamp(datetime.datetime.now() + expiration)

        # memory cache
        await self._set_mem_cache(endpoint, checksum, expires, data)

        # db cache
        if not self._exit:
            await self._set_db_cache(endpoint, checksum, expires, data)

        # remove this task from list
        self._busy_tasks.remove(task_name)

    async def _get_mem_cache(self, endpoint, checksum, cur_time):
        '''
            get cache data from memory cache
        '''
        result = None
        cachedata = self._mem_cache.get(endpoint)
        if cachedata:
            cachedata = cachedata
            if cachedata[0] > cur_time:
                if checksum == None or checksum == cachedata[2]:
                    result = cachedata[1]
        return result

    async def _set_mem_cache(self, endpoint, checksum, expires, data):
        '''
            put data in memory cache
        '''
        cachedata = (expires, data, checksum)
        self._mem_cache[endpoint] = cachedata

    async def _get_db_cache(self, endpoint, checksum, cur_time):
        '''get cache data from sqllite database'''
        result = None
        query = "SELECT expires, data, checksum FROM simplecache WHERE id = ?"
        cache_data = self._execute_sql(query, (endpoint,))
        if cache_data:
            cache_data = cache_data.fetchone()
            if cache_data and cache_data[0] > cur_time:
                if checksum == None or cache_data[2] == checksum:
                    result = eval(cache_data[1])
                    # also set result in memory cache for further access
                    await self._set_mem_cache(endpoint, checksum, cache_data[0], result)
        return result

    async def _set_db_cache(self, endpoint, checksum, expires, data):
        ''' store cache data in _database '''
        query = "INSERT OR REPLACE INTO simplecache( id, expires, data, checksum) VALUES (?, ?, ?, ?)"
        data = repr(data)
        self._execute_sql(query, (endpoint, expires, data, checksum))

    @run_periodic(3600)
    async def _do_cleanup(self):
        '''perform cleanup task'''
        if self._exit:
            return
        self._busy_tasks.append(__name__)
        cur_time = datetime.datetime.now()
        cur_timestamp = self._get_timestamp(cur_time)
        LOGGER.debug("Running cleanup...")
        query = "SELECT id, expires FROM simplecache"
        for cache_data in self._execute_sql(query).fetchall():
            cache_id = cache_data[0]
            cache_expires = cache_data[1]
            if self._exit:
                return
            # always cleanup all memory objects on each interval
            self._mem_cache.pop(cache_id, None)
            # clean up db cache object only if expired
            if cache_expires < cur_timestamp:
                query = 'DELETE FROM simplecache WHERE id = ?'
                self._execute_sql(query, (cache_id,))
                LOGGER.debug("delete from db %s" % cache_id)

        # compact db
        self._execute_sql("VACUUM")

        # remove task from list
        self._busy_tasks.remove(__name__)
        LOGGER.debug("Auto cleanup done")

    def _get_database(self):
        '''get reference to our sqllite _database - performs basic integrity check'''
        dbfile = "/tmp/simplecache.db"
        try:
            connection = sqlite3.connect(dbfile, timeout=30, isolation_level=None)
            connection.execute('SELECT * FROM simplecache LIMIT 1')
            return connection
        except Exception as error:
            # our _database is corrupt or doesn't exist yet, we simply try to recreate it
            if os.path.isfile(dbfile):
                os.remove(dbfile)
            try:
                connection = sqlite3.connect(dbfile, timeout=30, isolation_level=None)
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS simplecache(
                    id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)""")
                return connection
            except Exception as error:
                LOGGER.warning("Exception while initializing _database: %s" % str(error))
                return None

    def _execute_sql(self, query, data=None):
        '''little wrapper around execute and executemany to just retry a db command if db is locked'''
        retries = 0
        result = None
        error = None
        # always use new db object because we need to be sure that data is available for other simplecache instances
        with self._get_database() as _database:
            while not retries == 10:
                if self._exit:
                    return None
                try:
                    if isinstance(data, list):
                        result = _database.executemany(query, data)
                    elif data:
                        result = _database.execute(query, data)
                    else:
                        result = _database.execute(query)
                    return result
                except sqlite3.OperationalError as error:
                    if "_database is locked" in error:
                        LOGGER.debug("retrying DB commit...")
                        retries += 1
                        time.sleep(0.5)
                    else:
                        break
                except Exception as error:
                    LOGGER.error("_database ERROR ! -- %s" % str(error))
                    break
        return None

    @staticmethod
    def _get_timestamp(date_time):
        '''Converts a datetime object to unix timestamp'''
        return int(time.mktime(date_time.timetuple()))

    @staticmethod
    def _get_checksum(stringinput):
        '''get int checksum from string'''
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
                        cache_str += ".%s%s" %(subkey,subvalue)
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
                        cache_str += ".%s%s" %(subkey,subvalue)
                else:
                    cache_str += ".%s%s" %(key,value)
            cache_str = cache_str.lower()
            cachedata = await method_class.cache.get(cache_str, checksum=cache_checksum)
            if cachedata is not None:
                return cachedata
            else:
                result = await func(*args, **kwargs)
                await method_class.cache.set(cache_str, result, checksum=cache_checksum, expiration=datetime.timedelta(days=cache_days, hours=cache_hours))
                return result
        return wrapped
    return wrapper
