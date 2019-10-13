#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
import socket
import os
LOGGER = logging.getLogger()


def run_periodic(period):
    def scheduler(fcn):
        async def wrapper(*args, **kwargs):
            while True:
                asyncio.create_task(fcn(*args, **kwargs))
                await asyncio.sleep(period)
        return wrapper
    return scheduler

async def try_supported(task):
    ''' try to execute a task and pass NotImplementedError Exception '''
    ret = None
    try:
        ret = await task
    except NotImplementedError:
        pass
    return ret

def run_background_task(executor, corofn, *args):
    ''' run non-async task in background '''
    return asyncio.get_event_loop().run_in_executor(executor, corofn, *args)

def run_async_background_task(executor, corofn, *args):
    ''' run async task in background '''
    def run_task(corofn, *args):
        LOGGER.debug('running %s in background task' % corofn.__name__)
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        coro = corofn(*args)
        res = new_loop.run_until_complete(coro)
        new_loop.close()
        LOGGER.debug('completed %s in background task' % corofn.__name__)
        return res
    return asyncio.get_event_loop().run_in_executor(executor, run_task, corofn, *args)

def get_sort_name(name):
    ''' create a sort name for an artist/title '''
    sort_name = name
    for item in ["The ", "De ", "de ", "Les "]:
        if name.startswith(item):
            sort_name = "".join(name.split(item)[1:])
    return sort_name

def try_parse_int(possible_int):
    try:
        return int(possible_int)
    except:
        return 0

def try_parse_float(possible_float):
    try:
        return float(possible_float)
    except:
        return 0.0

def try_parse_bool(possible_bool):
    if isinstance(possible_bool, bool):
        return possible_bool
    else:
        return possible_bool in ['true', 'True', '1', 'on', 'ON', 1]

def parse_track_title(track_title):
    ''' try to parse clean track title and version from the title '''
    track_title = track_title.lower()
    title = track_title
    version = ''
    for splitter in [" (", " [", " - ", " (", " [", "-"]:
        if splitter in title:
            title_parts = title.split(splitter)
            for title_part in title_parts:
                # look for the end splitter
                for end_splitter in [")", "]"]:
                    if end_splitter in title_part:
                        title_part = title_part.split(end_splitter)[0]
                for ignore_str in ["feat.", "featuring", "ft.", "with ", " & "]:
                    if ignore_str in title_part:
                        title = title.split(splitter+title_part)[0]
                for version_str in ["version", "live", "edit", "remix", "mix", 
                            "acoustic", " instrumental", "karaoke", "remaster", "versie", "explicit", "radio", "unplugged", "disco"]:
                    if version_str in title_part:
                        version = title_part
                        title = title.split(splitter+version)[0]
    title = title.strip().title()
    # version substitutes
    if "radio" in version:
        version = "radio version"
    elif "album" in version:
        version = "album version"
    elif "single" in version:
        version = "single version"
    elif "remaster" in version:
        version = "remaster"
    version = version.strip().title()
    return title, version

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def get_hostname():
    return socket.gethostname()

def get_folder_size(folderpath):
    ''' get folder size in gb'''
    total_size = 0
    for dirpath, dirnames, filenames in os.walk(folderpath):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp)
    total_size_gb = total_size/float(1<<30)
    return total_size_gb