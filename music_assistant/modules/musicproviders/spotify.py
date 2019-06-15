#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
import sys
import time
from utils import run_periodic, LOGGER, parse_track_title
from models import MusicProvider, MediaType, TrackQuality, AlbumType, Artist, Album, Track, Playlist
from constants import CONF_USERNAME, CONF_PASSWORD, CONF_ENABLED
from asyncio_throttle import Throttler
import json
import aiohttp
from modules.cache import use_cache
import concurrent

def setup(mass):
    ''' setup the provider'''
    enabled = mass.config["musicproviders"]['spotify'].get(CONF_ENABLED)
    username = mass.config["musicproviders"]['spotify'].get(CONF_USERNAME)
    password = mass.config["musicproviders"]['spotify'].get(CONF_PASSWORD)
    if enabled and username and password:
        spotify_provider = SpotifyProvider(mass, username, password)
        return spotify_provider
    return False

def config_entries():
    ''' get the config entries for this provider (list with key/value pairs)'''
    return [
        (CONF_ENABLED, False, CONF_ENABLED),
        (CONF_USERNAME, "", CONF_USERNAME), 
        (CONF_PASSWORD, "<password>", CONF_PASSWORD)
        ]

class SpotifyProvider(MusicProvider):
    

    def __init__(self, mass, username, password):
        self.name = 'Spotify'
        self.prov_id = 'spotify'
        self._cur_user = None
        self.mass = mass
        self.cache = mass.cache
        self.http_session = aiohttp.ClientSession(loop=mass.event_loop, connector=aiohttp.TCPConnector(verify_ssl=False))
        self.throttler = Throttler(rate_limit=1, period=1)
        self._username = username
        self._password = password
        self.__auth_token = {}

    async def search(self, searchstring, media_types=List[MediaType], limit=5):
        ''' perform search on the provider '''
        result = {
            "artists": [],
            "albums": [],
            "tracks": [],
            "playlists": []
        }
        searchtypes = []
        if MediaType.Artist in media_types:
            searchtypes.append("artist")
        if MediaType.Album in media_types:
            searchtypes.append("album")
        if MediaType.Track in media_types:
            searchtypes.append("track")
        if MediaType.Playlist in media_types:
            searchtypes.append("playlist")
        searchtype = ",".join(searchtypes)
        params = {"q": searchstring, "type": searchtype, "limit": limit }
        searchresult = await self.__get_data("search", params=params, cache_checksum="bla")
        if searchresult:
            if "artists" in searchresult:
                for item in searchresult["artists"]["items"]:
                    artist = await self.__parse_artist(item)
                    if artist:
                        result["artists"].append(artist)
            if "albums" in searchresult:
                for item in searchresult["albums"]["items"]:
                    album = await self.__parse_album(item)
                    if album:
                        result["albums"].append(album)
            if "tracks" in searchresult:
                for item in searchresult["tracks"]["items"]:
                    track = await self.__parse_track(item)
                    if track:
                        result["tracks"].append(track)
            if "playlists" in searchresult:
                for item in searchresult["playlists"]["items"]:
                    playlist = await self.__parse_playlist(item)
                    if playlist:
                        result["playlists"].append(playlist)
        return result
    
    async def get_library_artists(self) -> List[Artist]:
        ''' retrieve library artists from spotify '''
        items = []
        spotify_artists = await self.__get_data("me/following?type=artist&limit=50")
        if spotify_artists:
            # TODO: use cursor method to retrieve more than 50 artists
            for artist_obj in spotify_artists['artists']['items']:
                prov_artist = await self.__parse_artist(artist_obj)
                items.append(prov_artist)
        return items
    
    async def get_library_albums(self) -> List[Album]:
        ''' retrieve library albums from the provider '''
        result = []
        for item in await self.__get_all_items("me/albums"):
            album = await self.__parse_album(item)
            if album:
                result.append(album)
        return result

    async def get_library_tracks(self) -> List[Track]:
        ''' retrieve library tracks from the provider '''
        result = []
        for item in await self.__get_all_items("me/tracks"):
            track = await self.__parse_track(item)
            if track:
                result.append(track)
        return result 

    async def get_playlists(self) -> List[Playlist]:
        ''' retrieve playlists from the provider '''
        result = []
        for item in await self.__get_all_items("me/playlists"):
            playlist = await self.__parse_playlist(item)
            if playlist:
                result.append(playlist)
        return result 

    async def get_artist(self, prov_artist_id) -> Artist:
        ''' get full artist details by id '''
        artist_obj = await self.__get_data("artists/%s" % prov_artist_id)
        return await self.__parse_artist(artist_obj)

    async def get_album(self, prov_album_id) -> Album:
        ''' get full album details by id '''
        album_obj = await self.__get_data("albums/%s" % prov_album_id)
        return await self.__parse_album(album_obj)

    async def get_track(self, prov_track_id) -> Track:
        ''' get full track details by id '''
        track_obj = await self.__get_data("tracks/%s" % prov_track_id)
        return await self.__parse_track(track_obj)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        ''' get full playlist details by id '''
        playlist_obj = await self.__get_data("playlists/%s" % prov_playlist_id, ignore_cache=True)
        return await self.__parse_playlist(playlist_obj)

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        ''' get album tracks for given album id '''
        track_objs = await self.__get_all_items("albums/%s/tracks" % prov_album_id)
        tracks = []
        for track_obj in track_objs:
            track = await self.__parse_track(track_obj)
            if track:
                tracks.append(track)
        return tracks

    async def get_playlist_tracks(self, prov_playlist_id, limit=50, offset=0) -> List[Track]:
        ''' get playlist tracks for given playlist id '''
        playlist_obj = await self.__get_data("playlists/%s?fields=snapshot_id" % prov_playlist_id, ignore_cache=True)
        cache_checksum = playlist_obj["snapshot_id"]
        track_objs = await self.__get_all_items("playlists/%s/tracks" % prov_playlist_id, limit=limit, offset=offset, cache_checksum=cache_checksum)
        tracks = []
        for track_obj in track_objs:
            playlist_track = await self.__parse_track(track_obj)
            if playlist_track:
                tracks.append(playlist_track)
        return tracks

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        ''' get a list of albums for the given artist '''
        params = {'include_groups': 'album,single,compilation'}
        items = await self.__get_all_items('artists/%s/albums' % prov_artist_id, params)
        albums = []
        for item in items:
            album = await self.__parse_album(item)
            if album:
                albums.append(album)
        return albums

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        ''' get a list of 10 most popular tracks for the given artist '''
        artist = await self.get_artist(prov_artist_id)
        items = await self.__get_data('artists/%s/top-tracks' % prov_artist_id)
        tracks = []
        for item in items['tracks']:
            track = await self.__parse_track(item)
            if track:
                track.artists = [artist]
                tracks.append(track)
        return tracks

    async def add_library(self, prov_item_id, media_type:MediaType):
        ''' add item to library '''
        if media_type == MediaType.Artist:
            result = await self.__put_data('me/following', {'ids': prov_item_id, 'type': 'artist'})
            item = await self.artist(prov_item_id)
        elif media_type == MediaType.Album:
            result = await self.__put_data('me/albums', {'ids': prov_item_id})
            item = await self.album(prov_item_id)
        elif media_type == MediaType.Track:
            result = await self.__put_data('me/tracks', {'ids': prov_item_id})
            item = await self.track(prov_item_id)
        await self.mass.db.add_to_library(item.item_id, media_type, self.prov_id)
        LOGGER.debug("added item %s to %s - %s" %(prov_item_id, self.prov_id, result))

    async def remove_library(self, prov_item_id, media_type:MediaType):
        ''' remove item from library '''
        if media_type == MediaType.Artist:
            result = await self.__delete_data('me/following', {'ids': prov_item_id, 'type': 'artist'})
            item = await self.artist(prov_item_id)
        elif media_type == MediaType.Album:
            result = await self.__delete_data('me/albums', {'ids': prov_item_id})
            item = await self.album(prov_item_id)
        elif media_type == MediaType.Track:
            result = await self.__delete_data('me/tracks', {'ids': prov_item_id})
            item = await self.track(prov_item_id)
        await self.mass.db.remove_from_library(item.item_id, media_type, self.prov_id)
        LOGGER.debug("deleted item %s from %s - %s" %(prov_item_id, self.prov_id, result))

    async def devices(self):
        ''' list all available devices '''
        items = await self.__get_data('me/player/devices')
        return items['devices']

    async def play_media(self, device_id, uri, offset_pos=None, offset_uri=None):
        ''' play uri on spotify device'''
        opts = {}
        if isinstance(uri, list):
            opts['uris'] = uri
        elif uri.startswith('spotify:track'):
            opts['uris'] = [uri]
        else:
            opts['context_uri'] = uri
        if offset_pos != None: # only for playlists/albums!
            opts["offset"] = {"position": offset_pos }
        elif offset_uri != None: # only for playlists/albums!
            opts["offset"] = {"uri": offset_uri }
        return await self.__put_data('me/player/play', {"device_id": device_id}, opts)

    async def get_stream_details(self, track_id):
        ''' return the content details for the given track when it will be streamed'''
        spotty = self.get_spotty_binary()
        spotty_exec = "%s -n temp -u %s -p %s --pass-through --single-track %s" %(spotty, self._username, self._password, track_id)
        return {
            "type": "executable",
            "path": spotty_exec,
            "content_type": "ogg",
            "sample_rate": 44100,
            "bit_depth": 16
        }
        
    async def __parse_artist(self, artist_obj):
        ''' parse spotify artist object to generic layout '''
        artist = Artist()
        artist.item_id = artist_obj['id']
        artist.provider = self.prov_id
        artist.provider_ids.append({
            "provider": self.prov_id,
            "item_id": artist_obj['id']
        })
        artist.name = artist_obj['name']
        if 'genres' in artist_obj:
            artist.tags = artist_obj['genres']
        if artist_obj.get('images'):
            for img in artist_obj['images']:
                img_url = img['url']
                if not '2a96cbd8b46e442fc41c2b86b821562f' in img_url:
                    artist.metadata["image"] = img_url
                    break
        if artist_obj.get('external_urls'):
            artist.metadata["spotify_url"] = artist_obj['external_urls']['spotify']
        return artist

    async def __parse_album(self, album_obj):
        ''' parse spotify album object to generic layout '''
        if 'album' in album_obj:
            album_obj = album_obj['album']
        if not album_obj['id'] or album_obj.get('is_playable') == False:
            return None
        album = Album()
        album.item_id = album_obj['id']
        album.provider = self.prov_id
        album.name, album.version = parse_track_title(album_obj['name'])
        for artist in album_obj['artists']:
            album.artist = await self.__parse_artist(artist)
            if album.artist:
                break
        if not album.artist:
            raise Exception("No album artist ! %s" % album_obj)
        if album_obj['album_type'] == 'single':
            album.albumtype = AlbumType.Single
        elif album_obj['album_type'] == 'compilation':
            album.albumtype = AlbumType.Compilation
        else:
            album.albumtype = AlbumType.Album
        if 'genres' in album_obj:
            album.tags = album_obj['genres']
        if album_obj.get('images'):
            album.metadata["image"] = album_obj['images'][0]['url']
        if 'external_ids' in album_obj:
            for key, value in album_obj['external_ids'].items():
                album.external_ids.append( { key: value } )
        if 'label' in album_obj:
            album.labels = album_obj['label'].split('/')
        if album_obj.get('release_date'):
            album.year = int(album_obj['release_date'].split('-')[0])
        if album_obj.get('copyrights'):
            album.metadata["copyright"] = album_obj['copyrights'][0]['text']
        if album_obj.get('external_urls'):
            album.metadata["spotify_url"] = album_obj['external_urls']['spotify']
        if album_obj.get('explicit'):
            album.metadata['explicit'] = str(album_obj['explicit']).lower()
        album.provider_ids.append({
            "provider": self.prov_id,
            "item_id": album_obj['id']
        })
        return album

    async def __parse_track(self, track_obj):
        ''' parse spotify track object to generic layout '''
        if 'track' in track_obj:
            track_obj = track_obj['track']
        if track_obj['is_local'] or not track_obj['id'] or not track_obj['is_playable']:
            LOGGER.warning("invalid/unavailable track found: %s - %s" % (track_obj.get('id'), track_obj.get('name')))
            return None
        track = Track()
        track.item_id = track_obj['id']
        track.provider = self.prov_id
        for track_artist in track_obj['artists']:
            artist = await self.__parse_artist(track_artist)
            if artist:
                track.artists.append(artist)
        track.name, track.version = parse_track_title(track_obj['name'])
        track.duration = track_obj['duration_ms'] / 1000
        track.metadata['explicit'] = str(track_obj['explicit']).lower()
        if not track.version and track_obj['explicit']:
            track.version = 'Explicit'
        if 'external_ids' in track_obj:
            for key, value in track_obj['external_ids'].items():
                track.external_ids.append( { key: value } )
        if 'album' in track_obj:
            track.album = await self.__parse_album(track_obj['album'])
        if track_obj.get('copyright'):
            track.metadata["copyright"] = track_obj['copyright']
        track.disc_number = track_obj['disc_number']
        track.track_number = track_obj['track_number']
        if track_obj.get('external_urls'):
            track.metadata["spotify_url"] = track_obj['external_urls']['spotify']
        track.provider_ids.append({
            "provider": self.prov_id,
            "item_id": track_obj['id'],
            "quality": TrackQuality.LOSSY_OGG
        })
        return track

    async def __parse_playlist(self, playlist_obj):
        ''' parse spotify playlist object to generic layout '''
        playlist = Playlist()
        if not playlist_obj.get('id'):
            return None
        playlist.item_id = playlist_obj['id']
        playlist.provider = self.prov_id
        playlist.provider_ids.append({
            "provider": self.prov_id,
            "item_id": playlist_obj['id']
        })
        playlist.name = playlist_obj['name']
        playlist.owner = playlist_obj['owner']['display_name']
        playlist.is_editable = playlist_obj['owner']['id'] == self.sp_user["id"] or playlist_obj['collaborative']
        if playlist_obj.get('images'):
            playlist.metadata["image"] = playlist_obj['images'][0]['url']
        if playlist_obj.get('external_urls'):
            playlist.metadata["spotify_url"] = playlist_obj['external_urls']['spotify']
        return playlist

    async def get_token(self):
        ''' get auth token on spotify '''
        # return existing token if we have one in memory
        if self.__auth_token and (self.__auth_token['expiresAt'] > int(time.time()) + 20):
            return self.__auth_token
        tokeninfo = {}
        if not self._username or not self._password:
            return tokeninfo
        # try with spotipy-token module first, fallback to spotty
        try:
            import spotify_token as st
            data = st.start_session(self._username, self._password)
            if data and len(data) == 2:
                tokeninfo = {"accessToken": data[0], "expiresIn": data[1] - int(time.time()), "expiresAt":data[1] }
        except Exception as exc:
            LOGGER.exception(exc)
        if not tokeninfo:
            # fallback to spotty approach
            import subprocess
            scopes = [
                "user-read-playback-state",
                "user-read-currently-playing",
                "user-modify-playback-state",
                "playlist-read-private",
                "playlist-read-collaborative",
                "playlist-modify-public",
                "playlist-modify-private",
                "user-follow-modify",
                "user-follow-read",
                "user-library-read",
                "user-library-modify",
                "user-read-private",
                "user-read-email",
                "user-read-birthdate",
                "user-top-read"]
            scope = ",".join(scopes)
            clientid = '2eb96f9b37494be1824999d58028a305'
            args = [self.get_spotty_binary(), "-t", "--client-id", clientid, "--scope", scope, "-n", "temp-spotty", "-u", self._username, "-p", self._password, "--disable-discovery"]
            spotty = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            stdout, stderr = spotty.communicate()
            result = json.loads(stdout)
            # transform token info to spotipy compatible format
            if result and "accessToken" in result:
                tokeninfo = result
                tokeninfo['expiresAt'] = tokeninfo['expiresIn'] + int(time.time())
        if tokeninfo:
            self.__auth_token = tokeninfo
            self.sp_user = await self.__get_data("me")
            LOGGER.info("Succesfully logged in to Spotify as %s" % self.sp_user["id"])
            self.__auth_token = tokeninfo
        else:
            raise Exception("Can't get Spotify token for user %s" % self._username)
        return tokeninfo

    async def __get_all_items(self, endpoint, params={}, limit=0, offset=0, cache_checksum=None):
        ''' get all items from a paged list '''
        if not cache_checksum:
            params["limit"] = 1
            params["offset"] = 0
            cache_checksum = await self.__get_data(endpoint, params, ignore_cache=True)
            cache_checksum = cache_checksum["total"]
        if limit:
            # partial listing
            params["limit"] = limit
            params["offset"] = offset
            result = await self.__get_data(endpoint, params=params, cache_checksum=cache_checksum)
            return result["items"]
        else:
            # full listing
            total_items = 1
            count = 0
            items = []
            while count < total_items:
                params["limit"] = 50
                params["offset"] = offset
                result = await self.__get_data(endpoint, params=params, cache_checksum=cache_checksum)
                total_items = result["total"]
                offset += 50
                count += len(result["items"])
                items += result["items"]
            return items

    @use_cache(7)
    async def __get_data(self, endpoint, params={}, ignore_cache=False, cache_checksum=None):
        ''' get data from api'''
        url = 'https://api.spotify.com/v1/%s' % endpoint
        params['market'] = 'from_token'
        params['country'] = 'from_token'
        token = await self.get_token()
        headers = {'Authorization': 'Bearer %s' % token["accessToken"]}
        async with self.throttler:
            async with self.http_session.get(url, headers=headers, params=params) as response:
                result = await response.json()
                if not result or 'error' in result:
                    LOGGER.error(url)
                    LOGGER.error(params)
                    result = None
                return result

    async def __delete_data(self, endpoint, params={}):
        ''' get data from api'''
        url = 'https://api.spotify.com/v1/%s' % endpoint
        token = await self.get_token()
        headers = {'Authorization': 'Bearer %s' % token["accessToken"]}
        async with self.http_session.delete(url, headers=headers, params=params) as response:
            return await response.text()

    async def __put_data(self, endpoint, params={}, data=None):
        ''' put data on api'''
        url = 'https://api.spotify.com/v1/%s' % endpoint
        token = await self.get_token()
        headers = {'Authorization': 'Bearer %s' % token["accessToken"]}
        async with self.http_session.put(url, headers=headers, params=params, json=data) as response:
            return await response.text()

    @staticmethod
    def get_spotty_binary():
        '''find the correct spotty binary belonging to the platform'''
        import platform
        sp_binary = None
        if platform.system() == "Windows":
            sp_binary = os.path.join(os.path.dirname(__file__), "spotty", "windows", "spotty.exe")
        elif platform.system() == "Darwin":
            # macos binary is x86_64 intel
            sp_binary = os.path.join(os.path.dirname(__file__), "spotty", "darwin", "spotty")
        elif platform.system() == "Linux":
            # try to find out the correct architecture by trial and error
            architecture = platform.machine()
            if architecture.startswith('AMD64') or architecture.startswith('x86_64'):
                # generic linux x86_64 binary
                sp_binary = os.path.join(os.path.dirname(__file__), "spotty", "x86-linux", "spotty-x86_64")
            else:
                sp_binary = os.path.join(os.path.dirname(__file__), "spotty", "arm-linux", "spotty-muslhf")
        return sp_binary


