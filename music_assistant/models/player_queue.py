#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
from typing import List
import operator
import random
import uuid
import os
import pickle

from ..utils import LOGGER, json, filename_from_string
from ..constants import CONF_ENABLED, EVENT_PLAYBACK_STARTED, EVENT_PLAYBACK_STOPPED, EVENT_QUEUE_UPDATED
from .media_types import Track, TrackQuality
from .playerstate import PlayerState


class QueueItem(Track):
    ''' representation of a queue item, simplified version of track '''
    def __init__(self, media_item=None):
        super().__init__()
        self.streamdetails = {}
        self.uri = ""
        self.queue_item_id = str(uuid.uuid4())
        # if existing media_item given, load those values
        if media_item:
            for key, value in media_item.__dict__.items():
                setattr(self, key, value)

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
        self._cur_index = 0
        self._cur_item_time = 0
        self._last_item_time = 0
        self._last_queue_startindex = 0
        self._last_player_state = PlayerState.Stopped
        self._save_busy_ = False
        self._last_track = None
        # load previous queue settings from disk
        self.mass.event_loop.run_in_executor(None, self.__load_from_file)
        
    @property
    def shuffle_enabled(self):
        return self._shuffle_enabled

    @property
    def repeat_enabled(self):
        return self._repeat_enabled

    @property
    def crossfade_enabled(self):
        return self._player.settings.get('crossfade_duration', 0) > 0

    @property
    def gapless_enabled(self):
        return self._player.settings.get('gapless_enabled', True)

    @property
    def cur_index(self):
        ''' match current uri with queue items to determine queue index '''
        if not self._items:
            return None
        return self._cur_index

    @property
    def cur_item(self):
        ''' return the queue item id of the current item in the queue '''
        if self.cur_index == None or not len(self.items) > self.cur_index:
            return None
        return self.items[self.cur_index].queue_item_id

    @property
    def cur_item_time(self):
        return self._cur_item_time
    
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
        if self.next_index != None:
            return self.items[self.next_index]
        return None
    
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
        if self.cur_index == None:
            return
        if self.use_queue_stream:
            return await self.play_index(self.cur_index+1)
        else:
            return await self._player.cmd_next()

    async def previous(self):
        ''' request previous track in queue '''
        if self.cur_index == None:
            return
        if self.use_queue_stream:
            return await self.play_index(self.cur_index-1)
        else:
            return await self._player.cmd_previous()

    async def resume(self):
        ''' resume previous queue '''
        if self.items:
            prev_index = self.cur_index
            if self.use_queue_stream or not self._player.supports_queue:
                await self.play_index(prev_index)
            else:
                # at this point we don't know if the queue is synced with the player
                # so just to be safe we send the queue_items to the player
                await self._player.cmd_queue_load(self.items)
                await self.play_index(prev_index)
        else:
            LOGGER.warning("resume queue requested for %s but queue is empty" % self._player.name)
    
    async def play_index(self, index):
        ''' play item at index X in queue '''
        if not len(self.items) > index:
            return
        if self.use_queue_stream:
            self._last_queue_startindex = index
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
        await self.mass.signal_event(EVENT_QUEUE_UPDATED, self._player.player_id)
        if self.use_queue_stream or not self._player.supports_queue:
            await self.play_index(0)
        else:
            await self._player.cmd_queue_load(queue_items)
        
    async def insert(self, queue_items:List[QueueItem], offset=0):
        ''' 
            insert new items at offset x from current position
            keeps remaining items in queue
            if offset 0, will start playing newly added item(s)
            :param queue_items: a list of QueueItem
            :param offset: offset from current queue position
        '''
        
        if not self.items or self.cur_index == None or self.cur_index + offset > len(self.items):
            return await self.load(queue_items)
        insert_at_index = self.cur_index + offset
        if self.shuffle_enabled:
            queue_items = await self.__shuffle_items(queue_items)
        self._items = self._items[:insert_at_index] + queue_items + self._items[insert_at_index:]
        self.mass.event_loop.create_task(
            self.mass.signal_event(EVENT_QUEUE_UPDATED, self._player.player_id))
        if self.use_queue_stream or not self._player.supports_queue:
            if offset == 0:
                return await self.play_index(insert_at_index)
        else:
            try:
                await self._player.cmd_queue_insert(queue_items, insert_at_index)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug("cmd_queue_insert not supported by player, fallback to cmd_queue_load ")
                return await self._player.cmd_queue_load(self._items[insert_at_index:])

    async def append(self, queue_items:List[QueueItem]):
        ''' 
            append new items at the end of the queue
        '''
        if self.shuffle_enabled:
            queue_items = await self.__shuffle_items(queue_items)
        self._items = self._items + queue_items
        self.mass.event_loop.create_task(
            self.mass.signal_event(EVENT_QUEUE_UPDATED, self._player.player_id))
        if self._player.supports_queue:
            try:
                return await self._player.cmd_queue_append(queue_items)
            except NotImplementedError:
                # not supported by player, use load queue instead
                LOGGER.debug("cmd_queue_append not supported by player, fallback to cmd_queue_load ")
                return await self._player.cmd_queue_load(self._items[self.cur_index:])

    async def update(self):
        ''' update queue details, called when player updates '''
        cur_index = self._cur_index
        track_time = self._cur_item_time
        # handle queue stream
        if self.use_queue_stream and self._player.state == PlayerState.Playing:
            cur_index, track_time = await self.__get_queue_stream_index()
        # normal queue based approach
        elif not self.use_queue_stream:
            track_time = self._player._cur_time
            for index, queue_item in enumerate(self.items):
                if queue_item.uri == self._player.cur_uri:
                    cur_index = index
                    break
        # process new index
        await self.__process_queue_update(cur_index, track_time)

    async def start_queue_stream(self):
        ''' called by the queue streamer when it starts playing the queue stream '''
        return await self.get_item(self._last_queue_startindex)

    async def __get_queue_stream_index(self):
        # player is playing a constant stream of the queue so we need to do this the hard way
        queue_index = 0
        cur_time_queue = self._player._cur_time
        total_time = 0
        track_time = 0
        if self.items and len(self.items) > self._last_queue_startindex:
            queue_index = self._last_queue_startindex # holds the last starting position
            queue_track = None
            while len(self.items) > queue_index:
                queue_track = self.items[queue_index]
                if cur_time_queue > (queue_track.duration + total_time):
                    total_time += queue_track.duration
                    queue_index += 1
                else:
                    track_time = cur_time_queue - total_time
                    break
        return queue_index, track_time
        
    async def __process_queue_update(self, new_index, track_time):
        ''' compare the queue index to determine if playback changed '''
        new_track = await self.get_item(new_index)
        if (not self._last_track and new_track) or self._last_track != new_track:
            # queue track updated
            # account for track changing state so trigger track change after 1 second
            if self._last_track and self._last_track.streamdetails:
                self._last_track.streamdetails["seconds_played"] = self._last_item_time
                await self.mass.signal_event(EVENT_PLAYBACK_STOPPED, self._last_track.streamdetails)
            if new_track and new_track.streamdetails:
                await self.mass.signal_event(EVENT_PLAYBACK_STARTED, new_track.streamdetails)
                self._last_track = new_track
            self.mass.event_loop.run_in_executor(None, self.__save_to_file)
        if self._last_player_state != self._player.state:
            self._last_player_state = self._player.state
            if (self._player.cur_time == 0 and 
                self._player.state in [PlayerState.Stopped, PlayerState.Off]):
                # player stopped playing
                self._last_queue_startindex = self.cur_index
        # update vars
        if track_time > 2:
            # account for track changing state so keep this a few seconds behind
            self._last_item_time = track_time
        self._cur_item_time = track_time
        self._cur_index = new_index
        
    async def __shuffle_items(self, queue_items):
        ''' shuffle a list of tracks '''
        # for now we use default python random function
        # can be extended with some more magic last last_played and stuff
        return random.sample(queue_items, len(queue_items))

    def __load_from_file(self):
        ''' try to load the saved queue for this player from file '''
        player_safe_str = filename_from_string(self._player.player_id)
        settings_dir = os.path.join(self.mass.datapath, 'queue')
        player_file = os.path.join(settings_dir, player_safe_str)
        if os.path.isfile(player_file):
            try:
                with open(player_file, 'rb') as f:
                    data = pickle.load(f)
                    self._shuffle_enabled = data["shuffle_enabled"]
                    self._repeat_enabled = data["repeat_enabled"]
                    self._items = data["items"]
                    self._cur_index = data["cur_item"]
                    self._last_queue_startindex = data["last_index"]
            except Exception as exc:
                LOGGER.debug("Could not load queue from disk - %s" % str(exc))

    def __save_to_file(self):
        ''' save current queue settings to file '''
        if self._save_busy_:
            return
        self._save_busy_ = True
        player_safe_str = filename_from_string(self._player.player_id)
        settings_dir = os.path.join(self.mass.datapath, 'queue')
        player_file = os.path.join(settings_dir, player_safe_str)
        data = {
            "shuffle_enabled": self._shuffle_enabled,
            "repeat_enabled": self._repeat_enabled,
            "items": self._items,
            "cur_item": self._cur_index,
            "last_index": self._cur_index
        }
        if not os.path.isdir(settings_dir):
            os.mkdir(settings_dir)
        with open(player_file, 'wb') as f:
            data = pickle.dump(data, f)
        self._save_busy_ = False


