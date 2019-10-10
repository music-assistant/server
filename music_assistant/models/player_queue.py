#!/usr/bin/env python3
# -*- coding:utf-8 -*-

from ..utils import LOGGER
from ..constants import CONF_ENABLED
from typing import List
from player import PlayerState
from media_types import Track, TrackQuality
import operator
import random

class QueueItem(object):
    ''' representation of a queue item, simplified version of track '''
    def __init__(self):
        self.item_id = None
        self.provider = None
        self.name = ''
        self.duration = 0
        self.version = ''
        self.quality = TrackQuality.FLAC_LOSSLESS
        self.metadata = {}
        self.artists = []
        self.album = None
        self.uri = ""
        self.is_radio = False
    def __eq__(self, other): 
        if not isinstance(other, self.__class__):
            return NotImplemented
        return (self.name == other.name and 
                self.version == other.version and
                self.item_id == other.item_id and
                self.provider == other.provider)
    def __ne__(self, other):
        return not self.__eq__(other)

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
        self._repeat_enabled = True
        self._cur_index = None

    @property
    def shuffle_enabled(self):
        return self._shuffle_enabled

    @property
    def repeat_enabled(self):
        return self._repeat_enabled

    @property
    def cur_index(self):
        return self._cur_index

    @property
    def cur_item(self):
        if self._cur_index == None:
            return None
        return self.mass.event_loop.run_until_complete(self.get_item(self._cur_index))

    @property
    async def next_index(self):
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
    async def next_item(self):
        ''' 
            return the next item in the queue
        '''
        return self.mass.event_loop.run_until_complete(
                self.get_item(self.next_index))
    
    @property
    async def items(self):
        ''' 
            return all queue items for this player 
        '''
        return self._items

    async def get_item(self, index):
        ''' get item by index from queue '''
        if len(self._items) > index:
            return self._items[index]
        return None

    async def shuffle(self, enable_shuffle:bool):
        ''' enable/disable shuffle '''
        if not self._shuffle_enabled and enable_shuffle:
            # shuffle requested
            self._shuffle_enabled = True
            self._items = await self.__shuffle_items(self._items)
            self._cur_index = None
            await self._player.play_queue()
            self.mass.event_loop.create_task(self._player.update())
        elif self._shuffle_enabled and not enable_shuffle:
            self._shuffle_enabled = False
            # TODO: Unshuffle the list ?
            self.mass.event_loop.create_task(self._player.update())
    
    async def load(self, queue_items:List[QueueItem]):
        ''' load (overwrite) queue with new items '''
        if self._shuffle_enabled:
            queue_items = await self.__shuffle_items(queue_items)
        self._items = queue_items
        self._cur_index = None
        await self._player.play_queue()

    async def insert(self, queue_items:List[QueueItem], offset=0):
        ''' 
            insert new items at offset x from current position
            keeps remaining items in queue
            if offset 0 or None, will start playing newly added item(s)
        '''
        insert_at_index = self.cur_index + offset
        if not self.items or insert_at_index >= len(self.items):
            return await self.load(queue_items)
        if self.shuffle_enabled:
            queue_items = await self.__shuffle_items(queue_items)
        self._items = self._items[:insert_at_index] + queue_items + self._items[insert_at_index:]
        if not offset:
            await self._player.stop()
            await self._player.play_queue()

    async def append(self, queue_items:List[QueueItem]):
        ''' 
            append new items at the end of the queue
        '''
        if self.shuffle_enabled:
            queue_items = await self.__shuffle_items(queue_items)
        self._items = self._items + queue_items

    async def __shuffle_items(self, queue_items):
        ''' shuffle a list of tracks '''
        # for now we use default python random function
        # can be extended with some more magic last last_played and stuff
        return random.sample(queue_items, len(queue_items))