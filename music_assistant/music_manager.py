#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import operator
import os
import base64
import functools
from typing import List
import toolz
from PIL import Image
import aiohttp

from .utils import run_periodic, LOGGER, load_provider_modules
from .models.media_types import MediaType, Track, Artist, Album, Playlist, Radio
from .constants import CONF_KEY_MUSICPROVIDERS, EVENT_MUSIC_SYNC_STATUS


def sync_task(desc):
    """ decorator to report a sync task """
    def wrapper(func):
        @functools.wraps(func)
        async def wrapped(*args):
            method_class = args[0]
            prov_id = args[1]
            # check if this sync task is not already running
            for sync_prov_id, sync_desc in method_class.running_sync_jobs:
                if sync_prov_id == prov_id and sync_desc == desc:
                    LOGGER.warning(
                        "Syncjob %s for provider %s is already running!", desc,
                        prov_id)
                    return
            sync_job = (prov_id, desc)
            if not method_class.running_sync_jobs:
                LOGGER.info("Music provider sync started")
            method_class.running_sync_jobs.append(sync_job)
            await method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs)
            await func(*args)
            LOGGER.info("Finished syncing %s for provider %s", desc, prov_id)
            method_class.running_sync_jobs.remove(sync_job)
            await method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs)
            if not method_class.running_sync_jobs:
                LOGGER.info("Music provider sync completed")

        return wrapped

    return wrapper


class MusicManager():
    ''' several helpers around the musicproviders '''
    def __init__(self, mass):
        self.running_sync_jobs = []
        self.mass = mass
        # dynamically load musicprovider modules
        self.providers = load_provider_modules(mass, CONF_KEY_MUSICPROVIDERS)

    async def setup(self):
        ''' async initialize of module '''
        # start providers
        for prov in self.providers.values():
            await prov.setup()
        # schedule sync task
        self.mass.event_loop.create_task(self.__sync_music_providers())

    async def item(self,
                   item_id,
                   media_type: MediaType,
                   provider='database',
                   lazy=True):
        ''' get single music item by id and media type'''
        if media_type == MediaType.Artist:
            return await self.artist(item_id, provider, lazy=lazy)
        elif media_type == MediaType.Album:
            return await self.album(item_id, provider, lazy=lazy)
        elif media_type == MediaType.Track:
            return await self.track(item_id, provider, lazy=lazy)
        elif media_type == MediaType.Playlist:
            return await self.playlist(item_id, provider)
        elif media_type == MediaType.Radio:
            return await self.radio(item_id, provider)
        else:
            return None

    async def library_artists(self,
                              limit=0,
                              offset=0,
                              orderby='name',
                              provider_filter=None) -> List[Artist]:
        ''' return all library artists, optionally filtered by provider '''
        return await self.mass.db.library_artists(provider=provider_filter,
                                                  limit=limit,
                                                  offset=offset,
                                                  orderby=orderby)

    async def library_albums(self,
                             limit=0,
                             offset=0,
                             orderby='name',
                             provider_filter=None) -> List[Album]:
        ''' return all library albums, optionally filtered by provider '''
        return await self.mass.db.library_albums(provider=provider_filter,
                                                 limit=limit,
                                                 offset=offset,
                                                 orderby=orderby)

    async def library_tracks(self,
                             limit=0,
                             offset=0,
                             orderby='name',
                             provider_filter=None) -> List[Track]:
        ''' return all library tracks, optionally filtered by provider '''
        return await self.mass.db.library_tracks(provider=provider_filter,
                                                 limit=limit,
                                                 offset=offset,
                                                 orderby=orderby)

    async def playlists(self,
                        limit=0,
                        offset=0,
                        orderby='name',
                        provider_filter=None) -> List[Playlist]:
        ''' return all library playlists, optionally filtered by provider '''
        return await self.mass.db.playlists(provider=provider_filter,
                                            limit=limit,
                                            offset=offset,
                                            orderby=orderby)

    async def radios(self,
                     limit=0,
                     offset=0,
                     orderby='name',
                     provider_filter=None) -> List[Playlist]:
        ''' return all library radios, optionally filtered by provider '''
        return await self.mass.db.radios(provider=provider_filter,
                                         limit=limit,
                                         offset=offset,
                                         orderby=orderby)

    async def library_items(self,
                            media_type: MediaType,
                            limit=0,
                            offset=0,
                            orderby='name',
                            provider_filter=None) -> List[object]:
        ''' get multiple music items in library'''
        if media_type == MediaType.Artist:
            return await self.library_artists(limit=limit,
                                              offset=offset,
                                              orderby=orderby,
                                              provider_filter=provider_filter)
        elif media_type == MediaType.Album:
            return await self.library_albums(limit=limit,
                                             offset=offset,
                                             orderby=orderby,
                                             provider_filter=provider_filter)
        elif media_type == MediaType.Track:
            return await self.library_tracks(limit=limit,
                                             offset=offset,
                                             orderby=orderby,
                                             provider_filter=provider_filter)
        elif media_type == MediaType.Playlist:
            return await self.playlists(limit=limit,
                                        offset=offset,
                                        orderby=orderby,
                                        provider_filter=provider_filter)
        elif media_type == MediaType.Radio:
            return await self.radios(limit=limit,
                                     offset=offset,
                                     orderby=orderby,
                                     provider_filter=provider_filter)

    async def artist(self, item_id, provider='database', lazy=True) -> Artist:
        ''' get artist by id '''
        if not provider or provider == 'database':
            return await self.mass.db.artist(item_id)
        return await self.providers[provider].artist(item_id, lazy=lazy)

    async def album(self, item_id, provider='database', lazy=True) -> Album:
        ''' get album by id '''
        if not provider or provider == 'database':
            return await self.mass.db.album(item_id)
        return await self.providers[provider].album(item_id, lazy=lazy)

    async def track(self, item_id, provider='database', lazy=True) -> Track:
        ''' get track by id '''
        if not provider or provider == 'database':
            return await self.mass.db.track(item_id)
        return await self.providers[provider].track(item_id, lazy=lazy)

    async def playlist(self, item_id, provider='database') -> Playlist:
        ''' get playlist by id '''
        if not provider or provider == 'database':
            return await self.mass.db.playlist(item_id)
        return await self.providers[provider].playlist(item_id)

    async def radio(self, item_id, provider='database') -> Radio:
        ''' get radio by id '''
        if not provider or provider == 'database':
            return await self.mass.db.radio(item_id)
        return await self.providers[provider].radio(item_id)

    async def playlist_by_name(self, name) -> Playlist:
        ''' get playlist by name '''
        for playlist in await self.playlists():
            if playlist.name == name:
                return playlist
        return None

    async def radio_by_name(self, name) -> Radio:
        ''' get radio by name '''
        for radio in await self.radios():
            if radio.name == name:
                return radio
        return None

    async def artist_toptracks(self, artist_id,
                               provider='database') -> List[Track]:
        ''' get top tracks for given artist '''
        artist = await self.artist(artist_id, provider)
        # always append database tracks
        items = await self.mass.db.artist_tracks(artist.item_id)
        for prov_mapping in artist.provider_ids:
            prov_id = prov_mapping['provider']
            prov_item_id = prov_mapping['item_id']
            prov_obj = self.providers[prov_id]
            items += [item async for item in prov_obj.artist_toptracks(prov_item_id)]
        items = list(toolz.unique(items, key=operator.attrgetter('item_id')))
        #items.sort(key=lambda x: x.name, reverse=False)
        return items

    async def artist_albums(self, artist_id,
                            provider='database') -> List[Album]:
        ''' get (all) albums for given artist '''
        artist = await self.artist(artist_id, provider)
        # always append database tracks
        items = await self.mass.db.artist_albums(artist.item_id)
        for prov_mapping in artist.provider_ids:
            prov_id = prov_mapping['provider']
            prov_item_id = prov_mapping['item_id']
            prov_obj = self.providers[prov_id]
            items += [item async for item in  prov_obj.artist_albums(prov_item_id)]
        items = list(toolz.unique(items, key=operator.attrgetter('item_id')))
        #items.sort(key=lambda x: x.name, reverse=False)
        return items

    async def album_tracks(self, album_id, provider='database') -> List[Track]:
        ''' get the album tracks for given album '''
        items = []
        album = await self.album(album_id, provider)
        # collect the tracks from the first provider
        prov = album.provider_ids[0]
        prov_obj = self.providers[prov['provider']]
        items = [item async for item in prov_obj.album_tracks(prov['item_id'])]
        items = sorted(items,
                       key=operator.attrgetter('disc_number', 'track_number'),
                       reverse=False)
        return items

    async def playlist_tracks(self,
                              playlist_id,
                              provider='database',
                              offset=0,
                              limit=50) -> List[Track]:
        ''' get the tracks for given playlist '''
        playlist = await self.playlist(playlist_id, provider)
        # return playlist tracks from provider
        prov = playlist.provider_ids[0]
        res = self.providers[prov['provider']
                                    ].playlist_tracks(prov['item_id'],
                                                      offset=offset,
                                                      limit=limit)
        return [item async for item in res]

    async def search(self,
                     searchquery,
                     media_types: List[MediaType],
                     limit=10,
                     online=False) -> dict:
        ''' search database or providers '''
        # get results from database
        result = await self.mass.db.search(searchquery, media_types, limit)
        if online:
            # include results from music providers
            for prov in self.providers.values():
                prov_results = await prov.search(searchquery, media_types,
                                                 limit)
                for item_type, items in prov_results.items():
                    if not item_type in result:
                        result[item_type] = items
                    else:
                        result[item_type] += items
            # filter out duplicates
            for item_type, items in result.items():
                items = list(
                    toolz.unique(items, key=operator.attrgetter('item_id')))
        return result

    async def item_action(self,
                          item_id,
                          media_type,
                          provider,
                          action,
                          action_details=None):
        ''' perform action on item (such as library add/remove) '''
        result = None
        item = await self.item(item_id, media_type, provider, lazy=False)
        if not item:
            return False
        if 'library_' in action:
            # remove or add item to the library
            for prov_mapping in item.provider_ids:
                prov_id = prov_mapping['provider']
                prov_item_id = prov_mapping['item_id']
                for prov in self.providers.values():
                    if prov.prov_id == prov_id:
                        if action == 'library_add':
                            result = await prov.add_library(
                                prov_item_id, media_type)
                            await self.mass.db.add_to_library(
                                item.item_id, item.media_type, prov_id)
                        elif action == 'library_remove':
                            result = await prov.remove_library(
                                prov_item_id, media_type)
                            await self.mass.db.remove_from_library(
                                item.item_id, item.media_type, prov_id)
        elif action == 'playlist_add':
            result = await self.add_playlist_tracks(action_details, [item])
        elif action == 'playlist_remove':
            result = await self.remove_playlist_tracks(action_details, [item])
        return result

    async def add_playlist_tracks(self, playlist_id, tracks: List[Track]):
        ''' add tracks to playlist - make sure we dont add dupes '''
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.playlist(playlist_id, 'database')
        if not playlist or not playlist.is_editable:
            LOGGER.warning(
                "Playlist %s is not editable - skip addition of tracks" %
                (playlist.name))
            return False
        # playlist can only have one provider (for now)
        playlist_prov = playlist.provider_ids[0]
        cur_playlist_tracks = await self.mass.db.playlist_tracks(playlist_id,
                                                                 limit=0)
        # grab all (database) track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = [item.item_id for item in cur_playlist_tracks]
        track_ids_to_add = []
        for index, track in enumerate(tracks):
            if not track.provider == 'database':
                # make sure we have a database track
                track = await self.track(track.item_id,
                                         track.provider,
                                         lazy=False)
            if track.item_id in cur_playlist_track_ids:
                LOGGER.warning(
                    "Track %s already in playlist %s - skip addition" %
                    (track.name, playlist.name))
                continue
            # we can only add a track to a provider playlist if the track is available on that provider
            # exception is the file provider which does accept tracks from all providers in the m3u playlist
            # this should all be handled in the frontend but these checks are here just to be safe
            track_playlist_provs = [
                item['provider'] for item in track.provider_ids
            ]
            if playlist_prov['provider'] in track_playlist_provs:
                # a track can contain multiple versions on the same provider
                # simply sort by quality and just add the first one (assuming the track is still available)
                track_versions = sorted(track.provider_ids,
                                        key=operator.itemgetter('quality'),
                                        reverse=True)
                for track_version in track_versions:
                    if track_version['provider'] == playlist_prov['provider']:
                        track_ids_to_add.append(track_version['item_id'])
                        break
            elif playlist_prov['provider'] == 'file':
                # the file provider can handle uri's from all providers in the file so simply add the db id
                track_ids_to_add.append(track.item_id)
            else:
                LOGGER.warning(
                    "Track %s not available on provider %s - skip addition to playlist %s"
                    % (track.name, playlist_prov['provider'], playlist.name))
                continue
        # actually add the tracks to the playlist on the provider
        if track_ids_to_add:
            return await self.providers[playlist_prov['provider']
                                        ].add_playlist_tracks(
                                            playlist_prov['item_id'],
                                            track_ids_to_add)

    async def remove_playlist_tracks(self, playlist_id, tracks: List[Track]):
        ''' remove tracks from playlist '''
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.playlist(playlist_id, 'database')
        if not playlist or not playlist.is_editable:
            LOGGER.warning(
                "Playlist %s is not editable - skip removal of tracks" %
                (playlist.name))
            return False
        # playlist can only have one provider (for now)
        prov_playlist = playlist.provider_ids[0]
        prov_playlist_playlist_id = prov_playlist['item_id']
        prov_playlist_provider_id = prov_playlist['provider']
        track_ids_to_remove = []
        for track in tracks:
            if not track.provider == 'database':
                # make sure we have a database track
                track = await self.track(track.item_id,
                                         track.provider,
                                         lazy=False)
            # a track can contain multiple versions on the same provider, remove all
            for track_provider in track.provider_ids:
                if track_provider['provider'] == prov_playlist_provider_id:
                    track_ids_to_remove.append(track_provider['item_id'])
        # actually remove the tracks from the playlist on the provider
        if track_ids_to_remove:
            return await self.providers[prov_playlist_provider_id
                                        ].remove_playlist_tracks(
                                            prov_playlist_playlist_id,
                                            track_ids_to_remove)

    @run_periodic(3600)
    async def __sync_music_providers(self):
        ''' periodic sync of all music providers '''
        for prov_id in self.providers:
            self.mass.event_loop.create_task(self.sync_music_provider(prov_id))

    async def sync_music_provider(self, prov_id: str):
        """
            Sync a music provider.
            param prov_id: {string} -- provider id to sync
        """
        await self.sync_library_albums(prov_id)
        await self.sync_library_tracks(prov_id)
        await self.sync_library_artists(prov_id)
        await self.sync_playlists(prov_id)
        await self.sync_radios(prov_id)

    @sync_task('artists')
    async def sync_library_artists(self, prov_id):
        ''' sync library artists for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.library_artists(provider_filter=prov_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_db_ids = []
        async for item in music_provider.get_library_artists():
            db_item = await music_provider.artist(item.item_id, lazy=False)
            cur_db_ids.append(db_item.item_id)
            if not db_item.item_id in prev_db_ids:
                await self.mass.db.add_to_library(db_item.item_id,
                                                  MediaType.Artist, prov_id)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_from_library(db_id, MediaType.Artist,
                                                       prov_id)

    @sync_task('albums')
    async def sync_library_albums(self, prov_id):
        ''' sync library albums for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.library_albums(provider_filter=prov_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_db_ids = []
        async for item in music_provider.get_library_albums():
            db_album = await music_provider.album(item.item_id, lazy=False)
            cur_db_ids.append(db_album.item_id)
            if not db_album.item_id in prev_db_ids:
                await self.mass.db.add_to_library(db_album.item_id,
                                                  MediaType.Album, prov_id)
            # precache album tracks
            [item async for item in music_provider.album_tracks(item.item_id)]
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_from_library(db_id, MediaType.Album,
                                                       prov_id)

    @sync_task('tracks')
    async def sync_library_tracks(self, prov_id):
        ''' sync library tracks for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.library_tracks(provider_filter=prov_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_db_ids = []
        async for item in music_provider.get_library_tracks():
            db_item = await music_provider.track(item.item_id, lazy=False)
            cur_db_ids.append(db_item.item_id)
            if not db_item.item_id in prev_db_ids:
                await self.mass.db.add_to_library(db_item.item_id,
                                                  MediaType.Track, prov_id)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_from_library(db_id, MediaType.Track,
                                                       prov_id)

    @sync_task('playlists')
    async def sync_playlists(self, prov_id):
        ''' sync library playlists for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.playlists(provider_filter=prov_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_db_ids = []
        async for item in music_provider.get_playlists():
            # always add to db because playlist attributes could have changed
            db_id = await self.mass.db.add_playlist(item)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.db.add_to_library(db_id, MediaType.Playlist,
                                                  prov_id)
        # process playlist deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_from_library(db_id,
                                                       MediaType.Playlist,
                                                       prov_id)

    @sync_task('radios')
    async def sync_radios(self, prov_id):
        ''' sync library radios for given provider'''
        music_provider = self.providers[prov_id]
        prev_items = await self.radios(provider_filter=prov_id)
        prev_db_ids = [item.item_id for item in prev_items]
        cur_db_ids = []
        async for item in music_provider.get_radios():
            db_id = await self.mass.db.get_database_id(prov_id, item.item_id,
                                                       MediaType.Radio)
            if not db_id:
                db_id = await self.mass.db.add_radio(item)
            cur_db_ids.append(db_id)
            if not db_id in prev_db_ids:
                await self.mass.db.add_to_library(db_id, MediaType.Radio,
                                                  prov_id)
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.db.remove_from_library(db_id, MediaType.Radio,
                                                       prov_id)

    async def get_image_path(self,
                             item_id,
                             media_type: MediaType,
                             provider,
                             size=50,
                             key='image'):
        ''' get path to (resized) thumb image for given media item '''
        cache_folder = os.path.join(self.mass.datapath, '.thumbs')
        cache_id = f'{item_id}{media_type}{provider}{key}'
        cache_id = base64.b64encode(cache_id.encode('utf-8')).decode('utf-8')
        cache_file_org = os.path.join(cache_folder, f'{cache_id}0.png')
        cache_file_sized = os.path.join(cache_folder, f'{cache_id}{size}.png')
        if os.path.isfile(cache_file_sized):
            # return file from cache
            return cache_file_sized
        # no file in cache so we should get it
        img_url = ''
        # we only retrieve items that we already have in cache
        item = None
        if await self.mass.db.get_database_id(provider, item_id, media_type):
            item = await self.item(item_id, media_type, provider)
        if not item:
            return ''
        if item and item.metadata.get(key):
            img_url = item.metadata[key]
        elif media_type == MediaType.Track:
            # try album image instead for tracks
            return await self.get_image_path(item.album.item_id,
                                             MediaType.Album,
                                             item.album.provider, size, key)
        elif media_type == MediaType.Album:
            # try artist image instead for albums
            return await self.get_image_path(item.artist.item_id,
                                             MediaType.Artist,
                                             item.artist.provider, size, key)
        if not img_url:
            return None
        # fetch image and store in cache
        os.makedirs(cache_folder, exist_ok=True)
        # download base image
        async with aiohttp.ClientSession() as session:
            async with session.get(img_url, verify_ssl=False) as response:
                assert response.status == 200
                img_data = await response.read()
                with open(cache_file_org, 'wb') as f:
                    f.write(img_data)
        if not size:
            # return base image
            return cache_file_org
        # save resized image
        basewidth = size
        img = Image.open(cache_file_org)
        wpercent = (basewidth / float(img.size[0]))
        hsize = int((float(img.size[1]) * float(wpercent)))
        img = img.resize((basewidth, hsize), Image.ANTIALIAS)
        img.save(cache_file_sized)
        # return file from cache
        return cache_file_sized
