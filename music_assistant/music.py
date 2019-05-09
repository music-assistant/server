#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from utils import run_periodic, run_async_background_task, LOGGER
import aiohttp
from difflib import SequenceMatcher as Matcher
from models import MediaType
from typing import List
import toolz
import operator


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_PATH = os.path.join(BASE_DIR, "modules", "musicproviders" )

class Music():
    ''' several helpers around the musicproviders '''
    
    def __init__(self, mass):
        self.sync_running = False
        self.mass = mass
        self.providers = {}
        # dynamically load musicprovider modules
        self.load_music_providers()
        # schedule sync task
        mass.event_loop.create_task(self.sync_music_providers())

    async def item(self, item_id, media_type:MediaType, lazy=True):
        ''' get single music item by id and media type'''
        if media_type == MediaType.Artist:
            return await self.artist(item_id, lazy=lazy)
        elif media_type == MediaType.Album:
            return await self.album(item_id, lazy=lazy)
        elif media_type == MediaType.Track:
            return await self.track(item_id, lazy=lazy)
        elif media_type == MediaType.Playlist:
            return await self.playlist(item_id)
        else:
            return None

    async def library_artists(self, limit=0, offset=0, orderby='name', provider_filter=None):
        ''' return all library artists, optionally filtered by provider '''
        return await self.mass.db.library_artists(provider=provider_filter, limit=limit, offset=offset, orderby=orderby)

    async def library_albums(self, limit=0, offset=0, orderby='name', provider_filter=None):
        ''' return all library albums, optionally filtered by provider '''
        return await self.mass.db.library_albums(provider=provider_filter, limit=limit, offset=offset, orderby=orderby)

    async def library_tracks(self, limit=0, offset=0, orderby='name', provider_filter=None):
        ''' return all library tracks, optionally filtered by provider '''
        return await self.mass.db.library_tracks(provider=provider_filter, limit=limit, offset=offset, orderby=orderby)

    async def library_playlists(self, limit=0, offset=0, orderby='name', provider_filter=None):
        ''' return all library playlists, optionally filtered by provider '''
        return await self.mass.db.library_playlists(provider=provider_filter, limit=limit, offset=offset, orderby=orderby)

    async def library_items(self, media_type:MediaType, limit=0, offset=0, orderby='name', provider_filter=None):
        ''' get multiple music items in library'''
        if media_type == MediaType.Artist:
            return await self.library_artists(limit=limit, offset=offset, orderby=orderby, provider_filter=provider_filter)
        elif media_type == MediaType.Album:
            return await self.library_albums(limit=limit, offset=offset, orderby=orderby, provider_filter=provider_filter)
        elif media_type == MediaType.Track:
            return await self.library_tracks(limit=limit, offset=offset, orderby=orderby, provider_filter=provider_filter)
        elif media_type == MediaType.Playlist:
            return await self.library_playlists(limit=limit, offset=offset, orderby=orderby, provider_filter=provider_filter)

    async def artist(self, item_id, lazy=True):
        ''' get artist by id '''
        artist = await self.mass.db.artist(item_id)
        if artist:
            return artist
        # not a database id, probably a provider id
        for provider in self.providers.values():
            artist = await provider.artist(item_id, lazy=lazy)
            if artist:
                return artist
        raise Exception("Artist %s is not found" % item_id)

    async def album(self, item_id, lazy=True):
        ''' get album by id '''
        album = await self.mass.db.album(item_id)
        if album:
            return album
        # not a database id, probably a provider id
        for provider in self.providers.values():
            album = await provider.album(item_id, lazy=lazy)
            if album:
                return album
        raise Exception("Album %s is not found" % item_id)

    async def track(self, item_id, lazy=True):
        ''' get track by id '''
        track = await self.mass.db.track(item_id)
        if track:
            return track
        # not a database id, probably a provider id
        for provider in self.providers.values():
            track = await provider.track(item_id, lazy=lazy)
            if track:
                return track
        raise Exception("Track %s is not found" % item_id)

    async def playlist(self, item_id):
        ''' get playlist by id '''
        playlist = await self.mass.db.playlist(item_id)
        if playlist:
            return playlist
        # not a database id, probably a provider id
        for provider in self.providers.values():
            playlist = await provider.playlist(item_id)
            if playlist:
                return playlist
        raise Exception("Playlist %s is not found" % item_id)

    async def artist_toptracks(self, artist_id):
        ''' get top tracks for given artist '''
        items = []
        artist = await self.artist(artist_id)
        # always append database tracks
        items += await self.mass.db.artist_tracks(artist.item_id)
        for prov_mapping in artist.provider_ids:
            prov_id = prov_mapping['provider']
            prov_item_id = prov_mapping['item_id']
            prov_obj = self.providers[prov_id]
            items += await prov_obj.artist_toptracks(prov_item_id)
        items = list(toolz.unique(items, key=operator.attrgetter('item_id')))
        items.sort(key=lambda x: x.name, reverse=False)
        return items

    async def artist_albums(self, artist_id):
        ''' get (all) albums for given artist '''
        items = []
        artist = await self.artist(artist_id)
        # always append database tracks
        items += await self.mass.db.artist_albums(artist.item_id)
        for prov_mapping in artist.provider_ids:
            prov_id = prov_mapping['provider']
            prov_item_id = prov_mapping['item_id']
            prov_obj = self.providers[prov_id]
            items += await prov_obj.artist_albums(prov_item_id)
        items = list(toolz.unique(items, key=operator.attrgetter('item_id')))
        items.sort(key=lambda x: x.name, reverse=False)
        return items

    async def album_tracks(self, album_id):
        ''' get the album tracks for given album '''
        items = []
        album = await self.album(album_id)
        for prov_mapping in album.provider_ids:
            prov_id = prov_mapping['provider']
            prov_item_id = prov_mapping['item_id']
            prov_obj = self.providers[prov_id]
            items += await prov_obj.album_tracks(prov_item_id)
        items = list(toolz.unique(items, key=operator.attrgetter('item_id')))
        return items

    async def playlist_tracks(self, playlist_id, offset=0, limit=50):
        ''' get the tracks for given playlist '''
        items = []
        playlist = await self.playlist(playlist_id)
        for prov_mapping in playlist.provider_ids:
            prov_id = prov_mapping['provider']
            prov_item_id = prov_mapping['item_id']
            prov_obj = self.providers[prov_id]
            items += await prov_obj.playlist_tracks(prov_item_id, offset=offset, limit=limit)
        items = list(toolz.unique(items, key=operator.attrgetter('item_id')))
        return items

    async def search(self, searchquery, media_types:List[MediaType], limit=10, online=False):
        ''' search database or providers '''
        # get results from database
        result = await self.mass.db.search(searchquery, media_types, limit)
        if online:
            # include results from music providers
            for prov in self.providers.values():
                prov_results = await prov.search(searchquery, media_types, limit)
                for item_type, items in prov_results.items():
                    result[item_type] += items
            # filter out duplicates
            for item_type, items in result.items():
                items = list(toolz.unique(items, key=operator.attrgetter('item_id')))
        return result

    async def item_action(self, item_id, media_type, action=None):
        ''' perform action on item (such as library add/remove) '''
        result = None
        item = await self.item(item_id, media_type)
        if item and action in ['add', 'remove']:
            # remove or add item to the library
            for prov_mapping in result.provider_ids:
                prov_id = prov_mapping['provider']
                prov_item_id = prov_mapping['item_id']
                for prov in self.providers.values():
                    if prov.prov_id == prov_id:
                        if action == 'add':
                            result = await prov.add_library(prov_item_id, media_type)
                        elif action == 'remove':
                            result = await prov.remove_library(prov_item_id, media_type)
        return result
    
    def get_music_provider(self, item_id):
        ''' get musicprovider object by id '''
        prov_obj = None
        if isinstance(item_id,int) or not '_' in item_id:
            prov_obj = self.mass.db
        else:
            prov_id = item_id.split('_')[0]
            item_id = item_id.split('_')[1]
            prov_obj = self.providers[prov_id]
        return item_id, prov_obj

    @run_periodic(3600)
    async def sync_music_providers(self):
        ''' periodic sync of all music providers '''
        if self.sync_running:
            return
        self.sync_running = True
        for prov_id in self.providers.keys():
            # sync library artists
            await self.sync_library_artists(prov_id)
            await self.sync_library_albums(prov_id)
            await self.sync_library_tracks(prov_id)
            await self.sync_library_playlists(prov_id)
        self.sync_running = False
        
    async def sync_library_artists(self, prov_id):
        ''' sync library artists for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.library_artists(provider_filter=prov_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_items = await music_provider.get_library_artists()
        cur_db_ids = []
        for item in cur_items:
            db_id = await self.mass.db.get_database_id(prov_id, item.item_id, MediaType.Artist)
            if db_id == None:
                db_id = await music_provider.add_artist(item)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.db.add_to_library(db_id, MediaType.Artist, prov_id)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_from_library(db_id, MediaType.Artist, prov_id)
        LOGGER.info("Finished syncing Artists for provider %s" % prov_id)

    async def sync_library_albums(self, prov_id):
        ''' sync library albums for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.library_albums(provider_filter=prov_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_items = await music_provider.get_library_albums()
        cur_db_ids = []
        for item in cur_items:
            db_id = await self.mass.db.get_database_id(prov_id, item.item_id, MediaType.Album)
            if db_id == None:
                db_id = await music_provider.add_album(item)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.db.add_to_library(db_id, MediaType.Album, prov_id)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_from_library(db_id, MediaType.Album, prov_id)
        LOGGER.info("Finished syncing Albums for provider %s" % prov_id)

    async def sync_library_tracks(self, prov_id):
        ''' sync library tracks for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.library_tracks(provider_filter=prov_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_items = await music_provider.get_library_tracks()
        cur_db_ids = []
        for item in cur_items:
            db_id = await self.mass.db.get_database_id(prov_id, item.item_id, MediaType.Track)
            if db_id == None:
                db_id = await music_provider.add_track(item)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.db.add_to_library(db_id, MediaType.Track, prov_id)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_from_library(db_id, MediaType.Track, prov_id)
        LOGGER.info("Finished syncing Tracks for provider %s" % prov_id)

    async def sync_library_playlists(self, prov_id):
        ''' sync library playlists for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.library_playlists(provider_filter=prov_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_items = await music_provider.get_library_playlists()
        cur_db_ids = []
        for item in cur_items:
            db_id = await self.mass.db.get_database_id(prov_id, item.item_id, MediaType.Playlist)
            if db_id == None:
                db_id = await music_provider.add_playlist(item)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.db.add_to_library(db_id, MediaType.Playlist, prov_id)
            # playlist tracks
            #asyncio.create_task( self.sync_playlist_tracks(db_id, prov_id, item.item_id) )
        # process playlist deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_from_library(db_id, MediaType.Playlist, prov_id)
        LOGGER.info("Finished syncing Playlists for provider %s" % prov_id)

    async def sync_playlist_tracks(self, db_playlist_id, prov_id, prov_playlist_id):
        ''' sync library playlists tracks for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.playlist_tracks(db_playlist_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_items = await music_provider.get_playlist_tracks(prov_playlist_id, limit=0)
        cur_db_ids = []
        pos = 0
        for item in cur_items:
            # we need to do this the complicated way because the file provider can return tracks from other providers
            for prov_mapping in item.provider_ids:
                item_prov_id = prov_mapping['provider']
                prov_item_id = prov_mapping['item_id']
                db_id = await self.mass.db.get_database_id(item_prov_id, prov_item_id, MediaType.Track)
                if db_id == None:
                    db_id = await self.providers[item_prov_id].add_track(item)
                if not db_id in cur_db_ids:
                    cur_db_ids.append(db_id)
                    await self.mass.db.add_playlist_track(db_playlist_id, db_id, pos)
            pos += 1
        # process playlist track deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_playlist_track(db_playlist_id, db_id)
        LOGGER.info("Finished syncing Playlist %s tracks for provider %s" % (prov_playlist_id, prov_id))

    def load_music_providers(self):
        ''' dynamically load musicproviders '''
        for item in os.listdir(MODULES_PATH):
            if (os.path.isfile(os.path.join(MODULES_PATH, item)) and not item.startswith("_") and 
                    item.endswith('.py') and not item.startswith('.')):
                module_name = item.replace(".py","")
                LOGGER.debug("Loading musicprovider module %s" % module_name)
                try:
                    mod = __import__("modules.musicproviders." + module_name, fromlist=[''])
                    if not self.mass.config['musicproviders'].get(module_name):
                        self.mass.config['musicproviders'][module_name] = {}
                    self.mass.config['musicproviders'][module_name]['__desc__'] = mod.config_entries()
                    for key, def_value, desc in mod.config_entries():
                        if not key in self.mass.config['musicproviders'][module_name]:
                            self.mass.config['musicproviders'][module_name][key] = def_value
                    mod = mod.setup(self.mass)
                    if mod:
                        self.providers[mod.prov_id] = mod
                        cls_name = mod.__class__.__name__
                        LOGGER.info("Successfully initialized module %s" % cls_name)
                except Exception as exc:
                    LOGGER.exception("Error loading module %s: %s" %(module_name, exc))
