#!/usr/bin/env python3
# -*- coding:utf-8 -*-

from typing import List
import datetime
import hashlib
import time
import aiohttp
from asyncio_throttle import Throttler

from ..utils import LOGGER, parse_title_and_version
from ..app_vars import get_app_var
from ..models import MusicProvider, MediaType, TrackQuality, \
    AlbumType, Artist, Album, Track, Playlist
from ..constants import CONF_USERNAME, CONF_PASSWORD, CONF_ENABLED, \
    CONF_TYPE_PASSWORD, EVENT_STREAM_STARTED, EVENT_PLAYBACK_STOPPED

PROV_ID = 'qobuz'
PROV_NAME = 'Qobuz'
PROV_CLASS = 'QobuzProvider'

CONFIG_ENTRIES = [(CONF_ENABLED, False, CONF_ENABLED),
                  (CONF_USERNAME, "", CONF_USERNAME),
                  (CONF_PASSWORD, CONF_TYPE_PASSWORD, CONF_PASSWORD)]


class QobuzProvider(MusicProvider):

    http_session = None
    throttler = None

    def __init__(self, mass, conf):
        ''' Support for streaming music provider Qobuz '''
        super().__init__(mass)
        self.name = PROV_NAME
        self.prov_id = PROV_ID
        self.__username = conf[CONF_USERNAME]
        self.__password = conf[CONF_PASSWORD]
        if not conf[CONF_USERNAME] or not conf[CONF_PASSWORD]:
            raise Exception("Username and password must not be empty")
        self.__user_auth_info = None
        self.__logged_in = False

    async def setup(self):
        ''' perform async setup '''
        self.http_session = aiohttp.ClientSession(
            loop=self.mass.event_loop, connector=aiohttp.TCPConnector())
        self.throttler = Throttler(rate_limit=4, period=1)
        await self.mass.add_event_listener(self.mass_event,
                                           EVENT_STREAM_STARTED)
        await self.mass.add_event_listener(self.mass_event,
                                           EVENT_PLAYBACK_STOPPED)

    async def search(self, searchstring, media_types=List[MediaType], limit=5):
        ''' perform search on the provider '''
        result = {"artists": [], "albums": [], "tracks": [], "playlists": []}
        params = {"query": searchstring, "limit": limit}
        if len(media_types) == 1:
            # qobuz does not support multiple searchtypes, falls back to all if no type given
            if media_types[0] == MediaType.Artist:
                params["type"] = "artists"
            if media_types[0] == MediaType.Album:
                params["type"] = "albums"
            if media_types[0] == MediaType.Track:
                params["type"] = "tracks"
            if media_types[0] == MediaType.Playlist:
                params["type"] = "playlists"
        searchresult = await self.__get_data("catalog/search", params)
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
                    result["playlists"].append(await
                                               self.__parse_playlist(item))
        return result

    async def get_library_artists(self) -> List[Artist]:
        ''' retrieve all library artists from qobuz '''
        params = {'type': 'artists'}
        endpoint = 'favorite/getUserFavorites'
        async for item in self.__get_all_items(endpoint, params, key='artists'):
            artist = await self.__parse_artist(item)
            if artist:
                yield artist

    async def get_library_albums(self) -> List[Album]:
        ''' retrieve all library albums from qobuz '''
        params = {'type': 'albums'}
        endpoint = 'favorite/getUserFavorites'
        async for item in self.__get_all_items(endpoint, params, key='albums'):
            album = await self.__parse_album(item)
            if album:
                yield album

    async def get_library_tracks(self) -> List[Track]:
        ''' retrieve library tracks from qobuz '''
        params = {'type': 'tracks'}
        endpoint = 'favorite/getUserFavorites'
        async for item in self.__get_all_items(endpoint, params, key='tracks'):
            track = await self.__parse_track(item)
            if track:
                yield track

    async def get_library_playlists(self) -> List[Playlist]:
        ''' retrieve all library playlists from the provider '''
        endpoint = 'favorite/getUserPlaylists'
        async for item in self.__get_all_items(endpoint, key='playlists'):
            playlist = await self.__parse_playlist(item)
            if playlist:
                yield playlist

    async def get_artist(self, prov_artist_id) -> Artist:
        ''' get full artist details by id '''
        params = {'artist_id': prov_artist_id}
        artist_obj = await self.__get_data("artist/get", params)
        return await self.__parse_artist(artist_obj)

    async def get_album(self, prov_album_id) -> Album:
        ''' get full album details by id '''
        params = {'album_id': prov_album_id}
        album_obj = await self.__get_data("album/get", params)
        return await self.__parse_album(album_obj)

    async def get_track(self, prov_track_id) -> Track:
        ''' get full track details by id '''
        params = {'track_id': prov_track_id}
        track_obj = await self.__get_data("track/get", params)
        return await self.__parse_track(track_obj)

    async def get_playlist(self, prov_playlist_id) -> Playlist:
        ''' get full playlist details by id '''
        params = {'playlist_id': prov_playlist_id}
        playlist_obj = await self.__get_data("playlist/get", params)
        return await self.__parse_playlist(playlist_obj)

    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        ''' get all album tracks for given album id '''
        params = {'album_id': prov_album_id}
        async for item in self.__get_all_items('album/get', params, key='tracks'):
            track = await self.__parse_track(item)
            if track:
                yield track
            else:
                LOGGER.warning("Unavailable track found in album %s: %s",
                               prov_album_id, item['title'])

    async def get_playlist_tracks(self, prov_playlist_id) -> List[Track]:
        ''' get all playlist tracks for given playlist id '''
        params = {'playlist_id': prov_playlist_id, 'extra': 'tracks'}
        endpoint = 'playlist/get'
        async for item in self.__get_all_items(endpoint, params, key='tracks'):
            playlist_track = await self.__parse_track(item)
            if playlist_track:
                yield playlist_track
            else:
                LOGGER.warning("Unavailable track found in playlist %s: %s",
                               prov_playlist_id, item['title'])
                # TODO: should we look for an alternative track version if the original is marked unavailable ?

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        ''' get a list of albums for the given artist '''
        params = { 'artist_id': prov_artist_id, 'extra': 'albums' }
        endpoint = 'artist/get'
        async for item in self.__get_all_items(endpoint, params, key='albums'):
            if str(item['artist']['id']) == str(prov_artist_id):
                album = await self.__parse_album(item)
                if album:
                    yield album

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        ''' get a list of most popular tracks for the given artist '''
        # artist toptracks not supported on Qobuz, so use search instead
        # assuming qobuz returns results sorted by popularity
        artist = await self.get_artist(prov_artist_id)
        params = {"query": artist.name, "limit": 25, "type": "tracks"}
        searchresult = await self.__get_data("catalog/search", params)
        for item in searchresult["tracks"]["items"]:
            if "performer" in item and str(
                    item["performer"]["id"]) == str(prov_artist_id):
                track = await self.__parse_track(item)
                if track:
                    yield track

    async def add_library(self, prov_item_id, media_type: MediaType):
        ''' add item to library '''
        if media_type == MediaType.Artist:
            result = await self.__get_data('favorite/create',
                                           {'artist_ids': prov_item_id})
            item = await self.artist(prov_item_id)
        elif media_type == MediaType.Album:
            result = await self.__get_data('favorite/create',
                                           {'album_ids': prov_item_id})
            item = await self.album(prov_item_id)
        elif media_type == MediaType.Track:
            result = await self.__get_data('favorite/create',
                                           {'track_ids': prov_item_id})
            item = await self.track(prov_item_id)
        await self.mass.db.add_to_library(item.item_id, media_type,
                                          self.prov_id)
        LOGGER.debug("added item %s to %s - %s",
                     prov_item_id, self.prov_id, result)

    async def remove_library(self, prov_item_id, media_type: MediaType):
        ''' remove item from library '''
        if media_type == MediaType.Artist:
            result = await self.__get_data('favorite/delete',
                                           {'artist_ids': prov_item_id})
            item = await self.artist(prov_item_id)
        elif media_type == MediaType.Album:
            result = await self.__get_data('favorite/delete',
                                           {'album_ids': prov_item_id})
            item = await self.album(prov_item_id)
        elif media_type == MediaType.Track:
            result = await self.__get_data('favorite/delete',
                                           {'track_ids': prov_item_id})
            item = await self.track(prov_item_id)
        await self.mass.db.remove_from_library(item.item_id, media_type,
                                               self.prov_id)
        LOGGER.debug("deleted item %s from %s - %s",
                     prov_item_id, self.prov_id, result)

    async def add_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        ''' add track(s) to playlist '''
        params = {
            'playlist_id': prov_playlist_id,
            'track_ids': ",".join(prov_track_ids)
        }
        return await self.__get_data('playlist/addTracks', params)

    async def remove_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        ''' remove track(s) from playlist '''
        playlist_track_ids = []
        params = {'playlist_id': prov_playlist_id, 'extra': 'tracks'}
        for track in await self.__get_all_items("playlist/get",
                                                params,
                                                key='tracks'):
            if track['id'] in prov_track_ids:
                playlist_track_ids.append(track['playlist_track_id'])
        params = {
            'playlist_id': prov_playlist_id,
            'track_ids': ",".join(playlist_track_ids)
        }
        return await self.__get_data('playlist/deleteTracks', params)

    async def get_stream_details(self, track_id):
        ''' return the content details for the given track when it will be streamed'''
        streamdetails = None
        for format_id in [27, 7, 6, 5]:
            # it seems that simply requesting for highest available quality does not work
            # from time to time the api response is empty for this request ?!
            params = {
                'format_id': format_id,
                'track_id': track_id,
                'intent': 'stream'
            }
            streamdetails = await self.__get_data('track/getFileUrl',
                                                  params,
                                                  sign_request=True)
            if streamdetails and streamdetails.get('url'):
                break
        if not streamdetails or not streamdetails.get('url'):
            LOGGER.error("Unable to retrieve stream url for track %s",
                         track_id)
            return None
        return {
            "type": "url",
            "path": streamdetails['url'],
            "content_type": streamdetails['mime_type'].split('/')[1],
            "sample_rate": int(streamdetails['sampling_rate'] * 1000),
            "bit_depth": streamdetails['bit_depth'],
            "details":
            streamdetails  # we need these details for reporting playback
        }

    async def mass_event(self, msg, msg_details):
        '''
            received event from mass
            we use this to report playback start/stop to qobuz
        '''
        if not self.__user_auth_info:
            return
        # TODO: need to figure out if the streamed track is purchased by user
        if msg == EVENT_STREAM_STARTED and msg_details[
                "provider"] == self.prov_id:
            # report streaming started to qobuz
            device_id = self.__user_auth_info["user"]["device"]["id"]
            credential_id = self.__user_auth_info["user"]["credential"]["id"]
            user_id = self.__user_auth_info["user"]["id"]
            format_id = msg_details["details"]["format_id"]
            timestamp = int(time.time())
            events = [{
                "online": True,
                "sample": False,
                "intent": "stream",
                "device_id": device_id,
                "track_id": msg_details["item_id"],
                "purchase": False,
                "date": timestamp,
                "credential_id": credential_id,
                "user_id": user_id,
                "local": False,
                "format_id": format_id
            }]
            await self.__post_data("track/reportStreamingStart", data=events)
        elif msg == EVENT_PLAYBACK_STOPPED and msg_details[
                "provider"] == self.prov_id:
            # report streaming ended to qobuz
            user_id = self.__user_auth_info["user"]["id"]
            params = {
                'user_id': user_id,
                'track_id': msg_details["item_id"],
                'duration': int(msg_details["seconds_played"])
            }
            await self.__get_data('/track/reportStreamingEnd', params)

    async def __parse_artist(self, artist_obj):
        ''' parse qobuz artist object to generic layout '''
        artist = Artist()
        if not artist_obj.get('id'):
            return None
        artist.item_id = artist_obj['id']
        artist.provider = self.prov_id
        artist.provider_ids.append({
            "provider": self.prov_id,
            "item_id": artist_obj['id']
        })
        artist.name = artist_obj['name']
        if artist_obj.get('image'):
            for key in ['extralarge', 'large', 'medium', 'small']:
                if artist_obj['image'].get(key):
                    if not '2a96cbd8b46e442fc41c2b86b821562f' in artist_obj[
                            'image'][key]:
                        artist.metadata["image"] = artist_obj['image'][key]
                        break
        if artist_obj.get('biography'):
            artist.metadata["biography"] = artist_obj['biography'].get(
                'content', '')
        if artist_obj.get('url'):
            artist.metadata["qobuz_url"] = artist_obj['url']
        return artist

    async def __parse_album(self, album_obj):
        ''' parse qobuz album object to generic layout '''
        album = Album()
        if not album_obj.get('id') or not album_obj[
                "streamable"] or not album_obj["displayable"]:
            # do not return unavailable items
            return None
        album.item_id = album_obj['id']
        album.provider = self.prov_id
        if album_obj['maximum_sampling_rate'] > 192:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_4
        elif album_obj['maximum_sampling_rate'] > 96:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_3
        elif album_obj['maximum_sampling_rate'] > 48:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_2
        elif album_obj['maximum_bit_depth'] > 16:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_1
        elif album_obj.get('format_id', 0) == 5:
            quality = TrackQuality.LOSSY_AAC
        else:
            quality = TrackQuality.FLAC_LOSSLESS
        album.provider_ids.append({
            "provider":
            self.prov_id,
            "item_id":
            album_obj['id'],
            "quality":
            quality,
            "details":
            "%skHz %sbit" % (album_obj['maximum_sampling_rate'],
                             album_obj['maximum_bit_depth'])
        })
        album.name, album.version = parse_title_and_version(
            album_obj['title'], album_obj.get('version'))
        album.artist = await self.__parse_artist(album_obj['artist'])
        if not album.artist:
            raise Exception("No album artist ! %s" % album_obj)
        if album_obj.get('product_type', '') == 'single':
            album.albumtype = AlbumType.Single
        elif album_obj.get(
                'product_type', ''
        ) == 'compilation' or 'Various' in album_obj['artist']['name']:
            album.albumtype = AlbumType.Compilation
        else:
            album.albumtype = AlbumType.Album
        if 'genre' in album_obj:
            album.tags = [album_obj['genre']['name']]
        if album_obj.get('image'):
            for key in ['extralarge', 'large', 'medium', 'small']:
                if album_obj['image'].get(key):
                    album.metadata["image"] = album_obj['image'][key]
                    break
        if len(album_obj['upc']) == 13:
            # qobuz writes ean as upc ?!
            album.external_ids.append({"ean": album_obj['upc']})
            album.external_ids.append({"upc": album_obj['upc'][1:]})
        else:
            album.external_ids.append({"upc": album_obj['upc']})
        if 'label' in album_obj:
            album.labels = album_obj['label']['name'].split('/')
        if album_obj.get('released_at'):
            album.year = datetime.datetime.fromtimestamp(
                album_obj['released_at']).year
        if album_obj.get('copyright'):
            album.metadata["copyright"] = album_obj['copyright']
        if album_obj.get('hires'):
            album.metadata["hires"] = "true"
        if album_obj.get('url'):
            album.metadata["qobuz_url"] = album_obj['url']
        if album_obj.get('description'):
            album.metadata["description"] = album_obj['description']
        return album

    async def __parse_track(self, track_obj):
        ''' parse qobuz track object to generic layout '''
        track = Track()
        if not track_obj.get('id') or not track_obj[
                "streamable"] or not track_obj["displayable"]:
            # do not return unavailable items
            return None
        track.item_id = track_obj['id']
        track.provider = self.prov_id
        if track_obj.get(
                'performer') and not 'Various ' in track_obj['performer']:
            artist = await self.__parse_artist(track_obj['performer'])
            if not artist:
                artist = self.get_artist(track_obj['performer']['id'])
            if artist:
                track.artists.append(artist)
        if not track.artists:
            # try to grab artist from album
            if track_obj.get('album') and track_obj['album'].get(
                    'artist'
            ) and not 'Various ' in track_obj['album']['artist']:
                artist = await self.__parse_artist(track_obj['album']['artist']
                                                   )
                if artist:
                    track.artists.append(artist)
        if not track.artists:
            # last resort: parse from performers string
            for performer_str in track_obj['performers'].split(' - '):
                role = performer_str.split(', ')[1]
                name = performer_str.split(', ')[0]
                if 'artist' in role.lower():
                    artist = Artist()
                    artist.name = name
                    artist.item_id = name
                track.artists.append(artist)
        # TODO: fix grabbing composer from details
        track.name, track.version = parse_title_and_version(
            track_obj['title'], track_obj.get('version'))
        track.duration = track_obj['duration']
        if 'album' in track_obj:
            album = await self.__parse_album(track_obj['album'])
            if album:
                track.album = album
        track.disc_number = track_obj['media_number']
        track.track_number = track_obj['track_number']
        if track_obj.get('hires'):
            track.metadata["hires"] = "true"
        if track_obj.get('url'):
            track.metadata["qobuz_url"] = track_obj['url']
        if track_obj.get('isrc'):
            track.external_ids.append({"isrc": track_obj['isrc']})
        if track_obj.get('performers'):
            track.metadata["performers"] = track_obj['performers']
        if track_obj.get('copyright'):
            track.metadata["copyright"] = track_obj['copyright']
        # get track quality
        if track_obj['maximum_sampling_rate'] > 192:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_4
        elif track_obj['maximum_sampling_rate'] > 96:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_3
        elif track_obj['maximum_sampling_rate'] > 48:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_2
        elif track_obj['maximum_bit_depth'] > 16:
            quality = TrackQuality.FLAC_LOSSLESS_HI_RES_1
        elif track_obj.get('format_id', 0) == 5:
            quality = TrackQuality.LOSSY_AAC
        else:
            quality = TrackQuality.FLAC_LOSSLESS
        track.provider_ids.append({
            "provider":
            self.prov_id,
            "item_id":
            track_obj['id'],
            "quality":
            quality,
            "details":
            "%skHz %sbit" % (track_obj['maximum_sampling_rate'],
                             track_obj['maximum_bit_depth'])
        })
        return track

    async def __parse_playlist(self, playlist_obj):
        ''' parse qobuz playlist object to generic layout '''
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
        playlist.owner = playlist_obj['owner']['name']
        playlist.is_editable = playlist_obj['owner'][
            'id'] == self.__user_auth_info["user"]["id"] or playlist_obj[
                'is_collaborative']
        if playlist_obj.get('images300'):
            playlist.metadata["image"] = playlist_obj['images300'][0]
        if playlist_obj.get('url'):
            playlist.metadata["qobuz_url"] = playlist_obj['url']
        playlist.checksum = playlist_obj['updated_at']
        return playlist

    async def __auth_token(self):
        ''' login to qobuz and store the token'''
        if self.__user_auth_info:
            return self.__user_auth_info["user_auth_token"]
        params = {
            "username": self.__username,
            "password": self.__password,
            "device_manufacturer_id": "music_assistant"
        }
        details = await self.__get_data("user/login", params)
        if details and "user" in details:
            self.__user_auth_info = details
            LOGGER.info("Succesfully logged in to Qobuz as %s",
                        details["user"]["display_name"])
            return details["user_auth_token"]

    async def __get_all_items(self, endpoint, params=None, key='tracks'):
        ''' get all items from a paged list '''
        if not params:
            params = {}
        limit = 50
        offset = 0
        while True:
            params["limit"] = limit
            params["offset"] = offset
            result = await self.__get_data(endpoint, params=params)
            offset += limit
            if not result or not key in result or not "items" in result[key]:
                break
            for item in result[key]['items']:
                yield item
            if len(result[key]['items']) < limit:
                break

    async def __get_data(self, endpoint, params=None, sign_request=False):
        ''' get data from api'''
        if not params:
            params = {}
        url = "http://www.qobuz.com/api.json/0.2/%s" % endpoint
        headers = {"X-App-Id": get_app_var(0)}
        if endpoint != 'user/login':
            auth_token = await self.__auth_token()
            if not auth_token:
                LOGGER.debug("Not logged in")
                return None
            headers["X-User-Auth-Token"] = auth_token
        if sign_request:
            signing_data = "".join(endpoint.split('/'))
            keys = list(params.keys())
            keys.sort()
            for key in keys:
                signing_data += "%s%s" % (key, params[key])
            request_ts = str(time.time())
            request_sig = signing_data + request_ts + get_app_var(1)
            request_sig = str(hashlib.md5(request_sig.encode()).hexdigest())
            params["request_ts"] = request_ts
            params["request_sig"] = request_sig
            params["app_id"] = get_app_var(0)
            params["user_auth_token"] = await self.__auth_token()
        async with self.throttler:
            async with self.http_session.get(url,
                                             headers=headers,
                                             params=params,
                                             verify_ssl=False) as response:
                try:
                    result = await response.json()
                    if "error" in result:
                        return None
                    return result
                except Exception as exc:
                    LOGGER.error(exc)
                    LOGGER.debug(url)
                    LOGGER.debug(params)
                    result = await response.text()
                    LOGGER.error(result)

    async def __post_data(self, endpoint, params=None, data=None):
        ''' post data to api'''
        if not params:
            params = {}
        if not data:
            data = {}
        url = "http://www.qobuz.com/api.json/0.2/%s" % endpoint
        params["app_id"] = get_app_var(0)
        params["user_auth_token"] = await self.__auth_token()
        async with self.http_session.post(url,
                                          params=params,
                                          json=data,
                                          verify_ssl=False) as response:
            try:
                result = await response.json()
                if "error" in result:
                    return None
                return result
            except Exception as exc:
                LOGGER.error(exc)
                LOGGER.debug(url)
                LOGGER.debug(params)
                result = await response.text()
                LOGGER.error(result)
