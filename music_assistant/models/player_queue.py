#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
from typing import List
import operator
import random
import uuid

from ..utils import LOGGER
from ..constants import CONF_ENABLED
from .media_types import Track, TrackQuality


class QueueItem(Track):
    ''' representation of a queue item, simplified version of track '''
    def __init__(self, media_item=None):
        super().__init__()
        self.quality = TrackQuality.FLAC_LOSSLESS
        self.uri = ""
        self.queue_item_id = str(uuid.uuid4())
        # if existing media_item given, load those values
        if media_item:
            for attribute, value in media_item.__dict__.items():
                setattr(self, attribute, value)

class PlayerQueue():
    ''' 
        basic implementation of a queue for a player 
        if no player specific queue exists, this will be used
    '''
    # TODO: Persistent storage in DB
    
    def __init__(self, mass, player):
        self.mass = mass
        self._player = player
        self._items = []
        self._shuffle_enabled = True
        self._repeat_enabled = False
        self._cur_index = None

    @property
    def shuffle_enabled(self):
        return self._shuffle_enabled

    @property
    def repeat_enabled(self):
        return self._repeat_enabled

    @property
    def crossfade_enabled(self):
        return self._player.settings['crossfade_duration']

    @property
    def gapless_enabled(self):
        return self._player.settings.get('gapless_enabled', True)

    @property
    def cur_index(self):
        ''' match current uri with queue items to determine queue index '''
        for index, queue_item in enumerate(self.items):
            if queue_item.uri == self._player.cur_uri:
                return index
        return self._cur_index

    @property
    def cur_item(self):
        if self.cur_index == None:
            return None
        return self.mass.bg_executor.submit(asyncio.run,self.get_item(self.cur_index)).result()

    @property
    def next_index(self):
        ''' 
            return the next queue index for this player
        '''
        if not self.items:
            # queue is empty
            return None
        if self.cur_index == None:
            # playback started
            return 0
        else:
            # player already playing (or paused) so return the next item
            if len(self.items) > (self.cur_index + 1):
                return self.cur_index + 1
            elif self._repeat_enabled:
                # repeat enabled, start queue at beginning
                return 0
        return None

    @property
    def next_item(self):
        ''' 
            return the next item in the queue
        '''
        return self.mass.bg_executor.submit(
                asyncio.run, self.get_item(self.next_index)).result()
    
    @property
    def items(self):
        ''' 
            return all queue items for this player 
        '''
        return self._items

    @property
    def use_queue_stream(self):
        ''' 
            bool to indicate that we need to use the queue stream
            for example if crossfading is requested but a player doesn't natively support it
            it will send a constant stream of audio to the player and all tracks
        '''
        return ((self.crossfade_enabled and not self._player.supports_crossfade) or 
            (self.gapless_enabled and not self._player.supports_gapless))
    
    async def get_item(self, index):
        ''' get item by index from queue '''
        if index != None and len(self.items) > index:
            return self.items[index]
        return None

    async def by_item_id(self, queue_item_id:str):
        ''' get item by queue_item_id from queue '''
        if not queue_item_id:
            return None
        for item in self.items:
            if item.queue_item_id == queue_item_id:
                return item
        return None
    
    async def shuffle(self, enable_shuffle:bool):
        ''' enable/disable shuffle '''
        if not self._shuffle_enabled and enable_shuffle:
            # shuffle requested
            self._shuffle_enabled = True
            await self.load(self._items)
            self.mass.event_loop.create_task(self._player.update())
        elif self._shuffle_enabled and not enable_shuffle:
            self._shuffle_enabled = False
            # TODO: Unshuffle the list ?
            self.mass.event_loop.create_task(self._player.update())
    
    async def next(self):
        ''' request next track in queue '''
        if self.use_queue_stream:
            return await self.play_index(self.cur_index+1)
        else:
            return await self._player.cmd_next()

    async def previous(self):
        ''' request previous track in queue '''
        if self.use_queue_stream:
            return await self.play_index(self.cur_index-1)
        else:
            return await self._player.cmd_previous()

    async def resume(self):
        ''' resume previous queue '''
        if self.items:
            prev_index = self.cur_index
            await self.load(self._items)
            if prev_index:
                await self.play_index(prev_index)
        else:
            LOGGER.warning("resume queue requested for %s but queue is empty" % self._player.name)
    
    async def play_index(self, index):
        ''' play item at index X in queue '''
        if not len(self.items) > index:
            return
        if self.use_queue_stream:
            self._cur_index = index -1
            queue_stream_uri = 'http://%s:%s/stream/%s'% (
                        self.mass.web.local_ip, self.mass.web.http_port, self._player.player_id)
            return await self._player.cmd_play_uri(queue_stream_uri)
        elif self._player.supports_queue:
            return await self._player.cmd_queue_play_index(index)
        else:
            return await self._player.cmd_play_uri(self._items[index].uri)
    
    async def load(self, queue_items:List[QueueItem]):
        ''' load (overwrite) queue with new items '''
        if self._shuffle_enabled:
            queue_items = await self.__shuffle_items(queue_items)
        self._items = queue_items
        self._cur_index = None
        if self.use_queue_stream or not self._player.supports_queue:
            return await self.play_index(0)
        else:
            return await self._player.cmd_queue_load(queue_items)

    async def insert(self, queue_items:List[QueueItem], offset=0):
        ''' 
            insert new items at offset x from current position
            keeps remaining items in queue
            if offset 0 or None, will start playing newly added item(s)
            :param queue_items: a list of QueueItem
            :param offset: offset from current queue position
        '''
        if self.cur_index:
            insert_at_index = self.cur_index + offset
        else:
            insert_at_index = 0
        if not self.items or insert_at_index >= len(self.items):
            return await self.load(queue_items)
        if self.shuffle_enabled:
            queue_items = await self.__shuffle_items(queue_items)
        self._items = self._items[:insert_at_index] + queue_items + self._items[insert_at_index:]
        if self.use_queue_stream or not self._player.supports_queue:
            if offset == 0:
                return await self.play_index(0)
        else:
            return await self._player.cmd_queue_insert(queue_items, offset)

    async def append(self, queue_items:List[QueueItem]):
        ''' 
            append new items at the end of the queue
        '''
        if self.shuffle_enabled:
            queue_items = await self.__shuffle_items(queue_items)
        self._items = self._items + queue_items
        if self._player.supports_queue:
            return await self._player.cmd_queue_append(queue_items)

    async def __shuffle_items(self, queue_items):
        ''' shuffle a list of tracks '''
        # for now we use default python random function
        # can be extended with some more magic last last_played and stuff
        return random.sample(queue_items, len(queue_items))