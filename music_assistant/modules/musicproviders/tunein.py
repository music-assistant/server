#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
import sys
import time
from utils import run_periodic, LOGGER, parse_track_title
from models import MusicProvider, MediaType, TrackQuality, Radio
from constants import CONF_USERNAME, CONF_PASSWORD, CONF_ENABLED
from asyncio_throttle import Throttler
import json
import aiohttp
from modules.cache import use_cache
import concurrent

def setup(mass):
    ''' setup the provider'''
    enabled = mass.config["musicproviders"]['tunein'].get(CONF_ENABLED)
    username = mass.config["musicproviders"]['tunein'].get(CONF_USERNAME)
    password = mass.config["musicproviders"]['tunein'].get(CONF_PASSWORD)
    if enabled and username and password:
        provider = TuneInProvider(mass, username, password)
        return provider
    return False

def config_entries():
    ''' get the config entries for this provider (list with key/value pairs)'''
    return [
        (CONF_ENABLED, False, CONF_ENABLED),
        (CONF_USERNAME, "", CONF_USERNAME), 
        (CONF_PASSWORD, "<password>", CONF_PASSWORD)
        ]

class TuneInProvider(MusicProvider):
    

    def __init__(self, mass, username, password):
        self.name = 'TuneIn Radio'
        self.prov_id = 'tunein'
        self.mass = mass
        self.cache = mass.cache
        self.http_session = aiohttp.ClientSession(loop=mass.event_loop, connector=aiohttp.TCPConnector(verify_ssl=False))
        self.throttler = Throttler(rate_limit=1, period=1)
        self._username = username
        self._password = password

    async def search(self, searchstring, media_types=List[MediaType], limit=5):
        ''' perform search on the provider '''
        result = {
            "artists": [],
            "albums": [],
            "tracks": [],
            "playlists": [],
            "radios": []
        }
        return result

    async def get_radios(self):
        ''' get favorited/library radio stations '''
        items = []
        params = {"c": "presets"}
        result = await self.__get_data("Browse.ashx", params, ignore_cache=True)
        if result and "body" in result:
            for item in result["body"]:
                # TODO: expand folders
                if item["type"] == "audio":
                    radio = await self.__parse_radio(item)
                    items.append(radio)
        return items

    async def get_radio(self, radio_id):
        ''' get radio station details '''
        radio = None
        params = {"c": "composite", "detail": "listing", "id": radio_id}
        result = await self.__get_data("Describe.ashx", params, ignore_cache=True)
        if result and result.get("body") and result["body"][0].get("children"):
            item = result["body"][0]["children"][0]
            radio = await self.__parse_radio(item)
        return radio

    async def __parse_radio(self, details):
        ''' parse Radio object from json obj returned from api '''
        radio = Radio()
        radio.item_id = details['preset_id']
        radio.provider = self.prov_id
        if "name" in details:
            radio.name = details["name"]
        else:
            # parse name from text attr
            name = details["text"]
            if " | " in name:
                name = name.split(" | ")[1]
            name = name.split(" (")[0]
            radio.name = name
        # parse stream urls and format
        stream_info = await self.__get_stream_urls(radio.item_id)
        for stream in stream_info["body"]:
            if stream["media_type"] == 'aac':
                quality = TrackQuality.LOSSY_AAC
            elif stream["media_type"] == 'ogg':
                quality = TrackQuality.LOSSY_OGG
            else:
                quality = TrackQuality.LOSSY_MP3
            radio.provider_ids.append({
                "provider": self.prov_id,
                "item_id": details['preset_id'],
                "quality": quality,
                "details": stream['url']
            })
        # image
        if "image" in details:
            radio.metadata["image"] = details["image"]
        elif "logo" in details:
            radio.metadata["image"] = details["logo"]
        return radio

    async def __get_stream_urls(self, radio_id):
        ''' get the stream urls for the given radio id '''
        params = {"id": radio_id}
        res = await self.__get_data("Tune.ashx", params)
        return res

    # async def get_stream_content_type(self, radio_id):
    #     ''' return the content type for the given radio when it will be streamed'''
    #     return 'flac' #TODO handle other file formats on qobuz?

    # async def get_audio_stream(self, track_id):
    #     ''' get audio stream for a track '''
    #     params = {'format_id': 27, 'track_id': track_id, 'intent': 'stream'}
    #     # we are called from other thread
    #     streamdetails_future = asyncio.run_coroutine_threadsafe(
    #         self.__get_data('track/getFileUrl', params, sign_request=True, ignore_cache=True),
    #         self.mass.event_loop
        
    @use_cache(7)
    async def __get_data(self, endpoint, params={}, ignore_cache=False, cache_checksum=None):
        ''' get data from api'''
        url = 'https://opml.radiotime.com/%s' % endpoint
        params['render'] = 'json'
        params['formats'] = 'ogg,aac,wma,mp3'
        params['username'] = self._username
        params['partnerId'] = '1'
        async with self.throttler:
            async with self.http_session.get(url, params=params) as response:
                result = await response.json()
                if not result or 'error' in result:
                    LOGGER.error(url)
                    LOGGER.error(params)
                    result = None
                return result

    