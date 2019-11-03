#!/usr/bin/python3
# -*- coding: utf-8 -*-

'''provides a simple stateless caching system'''

import datetime
import time
import sqlite3
from functools import reduce
import os
import functools
import asyncio

from .utils import run_periodic, LOGGER, parse_track_title

class Cache(object):
    '''basic stateless caching system '''
    # TODO: convert to aiosql
    _database = None

    def __init__(self, datapath):
        '''Initialize our caching class'''
        if not os.path.isdir(datapath):
            raise FileNotFoundError(f"data directory {datapath} does not exist!")
        self._datapath = datapath

    async def setup(self):
        ''' async initialize of cache module '''
        asyncio.get_running_loop().create_task(self._do_cleanup())

    async def get_async(self, endpoint, checksum=""):
        return await asyncio.get_running_loop().run_in_executor(None, self.get, endpoint, checksum)
    
    async def set_async(self, endpoint, data, checksum="", expiration=datetime.timedelta(days=14)):
        return await asyncio.get_running_loop().run_in_executor(None, self.set, endpoint, data, checksum, expiration)
    
    def get(self, endpoint, checksum=""):
        '''
            get object from cache and return the results
            endpoint: the (unique) name of the cache object as reference
            checkum: optional argument to check if the checksum in the cacheobject matches the checkum provided
        '''
        result = None
        cur_time = self._get_timestamp(datetime.datetime.now())
        query = "SELECT expires, data, checksum FROM simplecache WHERE id = ?"
        cache_data = self._execute_sql(query, (endpoint,))
        if cache_data:
            cache_data = cache_data.fetchone()
            if cache_data and cache_data[0] > cur_time:
                if checksum == None or cache_data[2] == checksum:
                    result = eval(cache_data[1])
        return result

    def set(self, endpoint, data, checksum="", expiration=datetime.timedelta(days=14)):
        '''
            set data in cache
        '''
        checksum = self._get_checksum(checksum)
        expires = self._get_timestamp(datetime.datetime.now() + expiration)
        query = "INSERT OR REPLACE INTO simplecache( id, expires, data, checksum) VALUES (?, ?, ?, ?)"
        data = repr(data)
        self._execute_sql(query, (endpoint, expires, data, checksum))

    @run_periodic(3600)
    async def auto_cleanup(self):
        ''' scheduled auto cleanup task '''
        asyncio.get_running_loop().run_in_executor(None, self._do_cleanup)

    async def _do_cleanup(self):
        '''perform cleanup task'''
        cur_time = datetime.datetime.now()
        cur_timestamp = self._get_timestamp(cur_time)
        LOGGER.debug("Running cleanup...")
        query = "SELECT id, expires FROM simplecache"
        for cache_data in self._execute_sql(query).fetchall():
            cache_id = cache_data[0]
            cache_expires = cache_data[1]
            # clean up db cache object only if expired
            if cache_expires < cur_timestamp:
                query = 'DELETE FROM simplecache WHERE id = ?'
                self._execute_sql(query, (cache_id,))
                LOGGER.debug("delete from db %s" % cache_id)
        # compact db
        self._execute_sql("VACUUM")
        LOGGER.debug("Auto cleanup done")

    def _get_database(self):
        '''get reference to our sqllite _database - performs basic integrity check'''
        dbfile = os.path.join(self._datapath, "simplecache.db")
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
            cachedata = await method_class.cache.get_async(cache_str, checksum=cache_checksum)
            if cachedata is not None:
                return cachedata
            else:
                result = await func(*args, **kwargs)
                await method_class.cache.set_async(cache_str, result, checksum=cache_checksum, expiration=datetime.timedelta(days=cache_days, hours=cache_hours))
                return result
        return wrapped
    return wrapper
