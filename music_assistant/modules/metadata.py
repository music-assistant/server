#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from music_assistant.utils import run_periodic, LOGGER
import json
import aiohttp
from asyncio_throttle import Throttler
from difflib import SequenceMatcher as Matcher
from music_assistant.modules.cache import use_cache
from yarl import URL
import re

LUCENE_SPECIAL = r'([+\-&|!(){}\[\]\^"~*?:\\\/])'

class MetaData():
    ''' several helpers to search and store mediadata for mediaitems '''
    
    def __init__(self, event_loop, db, cache):
        self.event_loop = event_loop
        self.db = db
        self.cache = cache
        self.musicbrainz = MusicBrainz(event_loop, cache)
        self.fanarttv = FanartTv(event_loop, cache)

    async def get_artist_metadata(self, mb_artist_id, cur_metadata):
        ''' get/update rich metadata for an artist by providing the musicbrainz artist id '''
        metadata = cur_metadata
        if not ('fanart' in metadata or 'thumb' in metadata):
            res = await self.fanarttv.artist_images(mb_artist_id)
            self.merge_metadata(cur_metadata, res)
        return metadata

    async def get_mb_artist_id(self, artistname, albumname=None, album_upc=None, trackname=None, track_isrc=None):
        ''' retrieve musicbrainz artist id for the given details '''
        LOGGER.debug('searching musicbrainz for %s (albumname: %s - album_upc: %s - trackname: %s - track_isrc: %s)' %(artistname, albumname, album_upc, trackname, track_isrc))
        mb_artist_id = None
        if album_upc:
            mb_artist_id = await self.musicbrainz.search_artist_by_album(artistname, None, album_upc)
        if not mb_artist_id and track_isrc:
            mb_artist_id = await self.musicbrainz.search_artist_by_track(artistname, None, track_isrc)
        if not mb_artist_id and albumname:
            mb_artist_id = await self.musicbrainz.search_artist_by_album(artistname, albumname)
        if not mb_artist_id and trackname:
            mb_artist_id = await self.musicbrainz.search_artist_by_track(artistname, trackname)
        LOGGER.debug('Got musicbrainz artist id for artist %s --> %s' %(artistname, mb_artist_id))
        return mb_artist_id

    @staticmethod
    def merge_metadata(cur_metadata, new_values):
        ''' merge new info into the metadata dict without overwiteing existing values '''
        for key, value in new_values.items():
            if not cur_metadata.get(key):
                cur_metadata[key] = value
        return cur_metadata

class MusicBrainz():

    def __init__(self, event_loop, cache):
        self.event_loop = event_loop
        self.cache = cache
        self.http_session = aiohttp.ClientSession(loop=event_loop, connector=aiohttp.TCPConnector(verify_ssl=False))
        self.throttler = Throttler(rate_limit=1, period=1)

    async def search_artist_by_album(self, artistname, albumname=None, album_upc=None):
        ''' retrieve musicbrainz artist id by providing the artist name and albumname or upc '''
        if album_upc:
            endpoint = 'release'
            params = {'query': 'barcode:%s' % album_upc}
        else:
            searchartist = re.sub(LUCENE_SPECIAL, r'\\\1', artistname)
            searchartist = searchartist.replace('/','').replace('\\','')
            searchalbum = re.sub(LUCENE_SPECIAL, r'\\\1', albumname)
            endpoint = 'release'
            params = {'query': 'artist:"%s" AND release:"%s"' % (searchartist, searchalbum)}
        result = await self.get_data(endpoint, params)
        if result and result.get('releases'):
            for strictness in [1, 0.95, 0.9]:
                for item in result['releases']:
                    if album_upc or Matcher(None, item['title'].lower(), albumname.lower()).ratio() >= strictness:
                        for artist in item['artist-credit']:
                            artist = artist['artist']
                            if Matcher(None, artist['name'].lower(), artistname.lower()).ratio() >= strictness:
                                return artist['id']
                            for item in artist.get('aliases',[]):
                                if item['name'].lower() == artistname.lower():
                                    return artist['id']
        return ''

    async def search_artist_by_track(self, artistname, trackname=None, track_isrc=None):
        ''' retrieve artist id by providing the artist name and trackname or track isrc '''
        endpoint = 'recording'
        searchartist = re.sub(LUCENE_SPECIAL, r'\\\1', artistname)
        searchartist = searchartist.replace('/','').replace('\\','')
        if track_isrc:
            endpoint = 'isrc/%s' % track_isrc
            params = {'inc': 'artist-credits'}
        else:
            searchtrack = re.sub(LUCENE_SPECIAL, r'\\\1', trackname)
            endpoint = 'recording'
            params = {'query': '"%s" AND artist:"%s"' % (searchtrack, searchartist)}
        result = await self.get_data(endpoint, params)
        if result and result.get('recordings'):
            for strictness in [1, 0.95]:
                for item in result['recordings']:
                    if track_isrc or Matcher(None, item['title'].lower(), trackname.lower()).ratio() >= strictness:
                        for artist in item['artist-credit']:
                            artist = artist['artist']
                            if Matcher(None, artist['name'].lower(), artistname.lower()).ratio() >= strictness:
                                return artist['id']
                            for item in artist.get('aliases',[]):
                                if item['name'].lower() == artistname.lower():
                                    return artist['id']
        return ''

    @use_cache(30)
    async def get_data(self, endpoint, params={}):
        ''' get data from api'''
        url = 'http://musicbrainz.org/ws/2/%s' % endpoint
        headers = {'User-Agent': 'Music Assistant/1.0.0 https://github.com/marcelveldt'}
        params['fmt'] = 'json'
        async with self.throttler:
            async with self.http_session.get(url, headers=headers, params=params) as response:
                try:
                    result = await response.json()
                except Exception as exc:
                    msg = await response.text()
                    LOGGER.exception("%s - %s" % (str(exc), msg))
                    result = None
                return result


class FanartTv():

    def __init__(self, event_loop, cache):
        self.event_loop = event_loop
        self.cache = cache
        self.http_session = aiohttp.ClientSession(loop=event_loop, connector=aiohttp.TCPConnector(verify_ssl=False))
        self.throttler = Throttler(rate_limit=1, period=1)

    async def artist_images(self, mb_artist_id):
        ''' retrieve images by musicbrainz artist id '''
        metadata = {}
        data = await self.get_data("music/%s" % mb_artist_id)
        if data:
            if data.get('hdmusiclogo'):
                metadata['logo'] = data['hdmusiclogo'][0]["url"]
            elif data.get('musiclogo'):
                metadata['logo'] = data['musiclogo'][0]["url"]
            if data.get('artistbackground'):
                count = 0
                for item in data['artistbackground']:
                    key = "fanart" if count == 0 else "fanart.%s" % count
                    metadata[key] = item["url"]
            if data.get('artistthumb'):
                url = data['artistthumb'][0]["url"]
                if not '2a96cbd8b46e442fc41c2b86b821562f' in url:
                    metadata['image'] = url
            if data.get('musicbanner'):
                metadata['banner'] = data['musicbanner'][0]["url"]
        return metadata

    @use_cache(30)
    async def get_data(self, endpoint, params={}):
        ''' get data from api'''
        url = 'http://webservice.fanart.tv/v3/%s' % endpoint
        params['api_key'] = '639191cb0774661597f28a47e7e2bad5'
        async with self.throttler:
            async with self.http_session.get(url, params=params) as response:
                result = await response.json()
                if 'error' in result and 'limit' in result['error']:
                    raise Exception(result['error'])
                return result
