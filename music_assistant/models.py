#!/usr/bin/env python3
# -*- coding:utf-8 -*-

from enum import Enum, IntEnum
from typing import List
import sys
sys.path.append("..")
from utils import run_periodic, LOGGER, parse_track_title
from difflib import SequenceMatcher as Matcher
import asyncio
from cache import use_cache


class MediaType(IntEnum):
    Artist = 1
    Album = 2
    Track = 3
    Playlist = 4

def media_type_from_string(media_type_str):
    media_type_str = media_type_str.lower()
    if 'artist' in media_type_str or media_type_str == '1':
        return MediaType.Artist
    elif 'album' in media_type_str or media_type_str == '2':
        return MediaType.Album
    elif 'track' in media_type_str or media_type_str == '3':
        return MediaType.Track
    elif 'playlist' in media_type_str or media_type_str == '4':
        return MediaType.Playlist
    else:
        return None

class ContributorRole(IntEnum):
    Artist = 1
    Writer = 2
    Producer = 3

class AlbumType(IntEnum):
    Album = 1
    Single = 2
    Compilation = 3

class TrackQuality(IntEnum):
    LOSSY_MP3 = 0
    LOSSY_OGG = 1
    LOSSY_AAC = 2
    FLAC_LOSSLESS = 6 # 44.1/48khz 16 bits HI-RES
    FLAC_LOSSLES_HI_RES_1 = 7 # 44.1/48khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_2 = 8 # 88.2/96khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_3 = 9 # 176/192khz 24 bits HI-RES
    FLAC_LOSSLESS_HI_RES_4 = 10 # above 192khz 24 bits HI-RES


class Artist(object):
    ''' representation of an artist '''
    def __init__(self):
        self.item_id = None
        self.name = ''
        self.sort_name = ''
        self.metadata = {}
        self.tags = []
        self.external_ids = []
        self.provider_ids = []
        self.media_type = MediaType.Artist
        self.in_library = []
        self.is_lazy = False

class Album(object):
    ''' representation of an album '''
    def __init__(self):
        self.item_id = None
        self.name = '' 
        self.metadata = {}
        self.version = ''
        self.external_ids = []
        self.tags = []
        self.albumtype = AlbumType.Album
        self.year = 0
        self.artist = None
        self.labels = []
        self.provider_ids = []
        self.media_type = MediaType.Album
        self.in_library = []
        self.is_lazy = False

class Track(object):
    ''' representation of a track '''
    def __init__(self):
        self.item_id = None
        self.name = ''
        self.duration = 0
        self.version = ''
        self.external_ids = []
        self.metadata = { }
        self.tags = []
        self.artists = []
        self.provider_ids = []
        self.album = None
        self.disc_number = 0
        self.track_number = 0
        self.media_type = MediaType.Track
        self.in_library = []
        self.is_lazy = False
    def __eq__(self, other): 
        if not isinstance(other, self.__class__):
            return NotImplemented
        return (self.name == other.name and 
                self.version == other.version and
                self.item_id == other.item_id)
    def __ne__(self, other):
        return not self.__eq__(other)

class Playlist(object):
    ''' representation of a playlist '''
    def __init__(self):
        self.item_id = None
        self.name = ''
        self.owner = ''
        self.provider_ids = []
        self.metadata = {}
        self.media_type = MediaType.Playlist
        self.in_library = []


class MusicProvider():
    ''' 
        Model for a Musicprovider
        Common methods usable for every provider
        Provider specific get methods shoud be overriden in the provider specific implementation
        Uses a form of lazy provisioning to local db as cache
    '''

    name = 'My great Music provider' # display name
    prov_id = 'my_provider' # used as id
    icon = ''

    def __init__(self, mass):
        self.mass = mass
        self.cache = mass.cache

    ### Common methods and properties ####

    async def artist(self, prov_item_id, lazy=True) -> Artist:
        ''' return artist details for the given provider artist id '''
        item_id = await self.mass.db.get_database_id(self.prov_id, prov_item_id, MediaType.Artist)
        if not item_id:
            # artist not yet in local database so fetch details
            artist_details = await self.get_artist(prov_item_id)
            if not artist_details:
                LOGGER.warning('artist not found: %s' % prov_item_id)
                return None
            if lazy:
                asyncio.create_task(self.add_artist(artist_details))
                artist_details.is_lazy = True
                return artist_details
            item_id = await self.add_artist(artist_details)
        return await self.mass.db.artist(item_id)

    async def add_artist(self, artist_details, skip_match=False) -> int:
        ''' add artist to local db and return the new database id'''
        musicbrainz_id = None
        for item in artist_details.external_ids:
            if item.get("musicbrainz"):
                musicbrainz_id = item["musicbrainz"]
        if not musicbrainz_id:
            musicbrainz_id = await self.get_artist_musicbrainz_id(artist_details, allow_fallback=not skip_match)
        if not musicbrainz_id:
            return
        # grab additional metadata
        if musicbrainz_id:
            artist_details.external_ids.append({"musicbrainz": musicbrainz_id})
            artist_details.metadata = await self.mass.metadata.get_artist_metadata(musicbrainz_id, artist_details.metadata)
        item_id = await self.mass.db.add_artist(artist_details)
        # also fetch same artist on all providers
        if not skip_match:
            new_artist = await self.mass.db.artist(item_id)
            item_provider_keys = [item['provider'] for item in new_artist.provider_ids]
            for prov_id, provider in self.mass.music.providers.items():
                if not prov_id in item_provider_keys:
                    await provider.match_artist(new_artist)
        return item_id

    async def get_artist_musicbrainz_id(self, artist_details:Artist, allow_fallback=False):
        ''' fetch musicbrainz id by performing search with both the artist and one of it's albums or tracks '''
        musicbrainz_id = ""
        # try with album first
        lookup_albums = await self.get_artist_albums(artist_details.item_id)
        for lookup_album in lookup_albums[:10]:
            lookup_album_upc = None
            for item in lookup_album.external_ids:
                if item.get("upc"):
                    lookup_album_upc = item["upc"]
                    break
            musicbrainz_id = await self.mass.metadata.get_mb_artist_id(artist_details.name, 
                    albumname=lookup_album.name, album_upc=lookup_album_upc)
            if musicbrainz_id:
                break
        # fallback to track
        lookup_tracks = await self.get_artist_toptracks(artist_details.item_id)
        for lookup_track in lookup_tracks[:10]:
            lookup_track_isrc = None
            for item in lookup_track.external_ids:
                if item.get("isrc"):
                    lookup_track_isrc = item["isrc"]
                    break
            musicbrainz_id = await self.mass.metadata.get_mb_artist_id(artist_details.name, 
                        trackname=lookup_track.name, track_isrc=lookup_track_isrc)
            if musicbrainz_id:
                break
        if not musicbrainz_id:
            LOGGER.warning("Unable to get musicbrainz ID for artist %s !" % artist_details.name)
            if allow_fallback:
                musicbrainz_id = artist_details.name
        return musicbrainz_id

    async def album(self, prov_item_id, lazy=True) -> Album:
        ''' return album details for the given provider album id'''
        item_id = await self.mass.db.get_database_id(self.prov_id, prov_item_id, MediaType.Album)
        if not item_id:
            # album not yet in local database so fetch details
            album_details = await self.get_album(prov_item_id)
            if not album_details:
                LOGGER.warning('album not found: %s' % prov_item_id)
                return album_details
            if lazy:
                asyncio.create_task(self.add_album(album_details))
                album_details.is_lazy = True
                return album_details
            item_id = await self.add_album(album_details)
        return await self.mass.db.album(item_id)

    async def add_album(self, album_details, skip_match=False) -> int:
        ''' add album to local db and return the new database id'''
        # we need to fetch album artist too
        db_album_artist = await self.artist(album_details.artist.item_id, lazy=False)
        album_details.artist = db_album_artist
        item_id = await self.mass.db.add_album(album_details)
        # also fetch same album on all providers
        if not skip_match:
            new_album = await self.mass.db.album(item_id)
            item_provider_keys = [item['provider'] for item in new_album.provider_ids]
            for prov_id, provider in self.mass.music.providers.items():
                if not prov_id in item_provider_keys:
                    await provider.match_album(new_album)
        return item_id

    async def track(self, prov_item_id, lazy=True, track_details=None) -> Track:
        ''' return track details for the given provider track id '''
        item_id = await self.mass.db.get_database_id(self.prov_id, prov_item_id, MediaType.Track)
        if not item_id:
            # album not yet in local database so fetch details
            if not track_details:
                track_details = await self.get_track(prov_item_id)
            if not track_details:
                LOGGER.warning('track not found: %s' % prov_item_id)
                return None
            if lazy:
                asyncio.ensure_future(self.add_track(track_details))
                track_details.is_lazy = True
                return track_details
            item_id = await self.add_track(track_details)
        return await self.mass.db.track(item_id)

    async def add_track(self, track_details, prov_album_id=None, skip_match=False) -> int:
        ''' add track to local db and return the new database id'''
        track_artists = []
        assert(track_details)
        # we need to fetch track artists too
        for track_artist in track_details.artists:
            prov_item_id = track_artist.item_id
            db_track_artist = await self.artist(prov_item_id, lazy=False)
            assert(db_track_artist)
            track_artists.append(db_track_artist)
        track_details.artists = track_artists
        if not prov_album_id:
            prov_album_id = track_details.album.item_id
        track_details.album = await self.album(prov_album_id, lazy=False)
        item_id = await self.mass.db.add_track(track_details)
        # also fetch same track on all providers
        if not skip_match:
            new_track = await self.mass.db.track(item_id)
            item_provider_keys = [item['provider'] for item in new_track.provider_ids]
            for prov_id, provider in self.mass.music.providers.items():
                if not prov_id in item_provider_keys:
                    await provider.match_track(new_track)
        return item_id
    
    async def playlist(self, prov_item_id) -> Playlist:
        ''' return playlist details for the given provider playlist id '''
        item_id = await self.mass.db.get_database_id(self.prov_id, prov_item_id, MediaType.Playlist)
        if item_id:
            return await self.mass.db.playlist(item_id)
        else:
            return await self.get_playlist(prov_item_id)

    async def add_playlist(self, playlist_details) -> int:
        ''' add playlist to local db and return the (new) database id'''
        item_id = await self.mass.db.add_playlist(playlist_details)
        return item_id

    async def album_tracks(self, prov_album_id) -> List[Track]:
        ''' return album tracks for the given provider album id'''
        items = []
        album = await self.get_album(prov_album_id)
        for prov_track in await self.get_album_tracks(prov_album_id):
            prov_track.album = album
            track = await self.track(prov_track.item_id, track_details=prov_track)
            items.append(track)
        return items

    async def playlist_tracks(self, prov_playlist_id, limit=100, offset=0) -> List[Track]:
        ''' return playlist tracks for the given provider playlist id'''
        items = []
        for prov_track in await self.get_playlist_tracks(prov_playlist_id, limit=limit, offset=offset):
            for prov_mapping in prov_track.provider_ids:
                item_prov_id = prov_mapping["provider"]
                prov_item_id = prov_mapping["item_id"]
                db_id = await self.mass.db.get_database_id(item_prov_id, prov_item_id, MediaType.Track) 
                if db_id:
                    items.append( await self.mass.db.track(db_id) )
                else:
                    items.append(prov_track)
                    asyncio.create_task(self.add_track(prov_track))
        return items
    
    async def artist_toptracks(self, prov_item_id) -> List[Track]:
        ''' return top tracks for an artist '''
        items = []
        for prov_track in await self.get_artist_toptracks(prov_item_id):
            db_id = await self.mass.db.get_database_id(self.prov_id, prov_track.item_id, MediaType.Track) 
            if db_id:
                items.append( await self.mass.db.track(db_id) )
            else:
                items.append(prov_track)
                asyncio.create_task(self.add_track(prov_track))
        return items

    async def artist_albums(self, prov_item_id) -> List[Track]:
        ''' return (all) albums for an artist '''
        items = []
        for prov_album in await self.get_artist_albums(prov_item_id):
            db_id = await self.mass.db.get_database_id(self.prov_id, prov_album.item_id, MediaType.Album) 
            if db_id:
                items.append( await self.mass.db.album(db_id) )
            else:
                items.append(prov_album)
                asyncio.create_task(self.add_album(prov_album))
        return items
    
    async def match_artist(self, searchartist:Artist):
        ''' try to match artist in this provider by supplying db artist '''
        for prov_mapping in searchartist.provider_ids:
            if prov_mapping["provider"] == self.prov_id:
                return # we already have a mapping on this provider
        search_results = await self.search(searchartist.name, [MediaType.Artist], limit=2)
        for item in search_results["artists"]:
            if item.name.lower() == searchartist.name.lower():
                # just lazy load this item in the database, it will be matched automagically ;-)
                db_id = await self.mass.db.get_database_id(self.prov_id, item.item_id, MediaType.Artist)
                if not db_id:
                    asyncio.create_task(self.add_artist(item, skip_match=True))

    async def match_album(self, searchalbum:Album):
        ''' try to match album in this provider by supplying db album '''
        for prov_mapping in searchalbum.provider_ids:
            if prov_mapping["provider"] == self.prov_id:
                return # we already have a mapping on this provider
        searchstr = "%s - %s %s" %(searchalbum.artist.name, searchalbum.name, searchalbum.version)
        search_results = await self.search(searchstr, [MediaType.Album], limit=5)
        for item in search_results["albums"]:
            if item.name == searchalbum.name and item.version == searchalbum.version and item.artist.name == searchalbum.artist.name:
                # just lazy load this item in the database, it will be matched automagically ;-)
                db_id = await self.mass.db.get_database_id(self.prov_id, item.item_id, MediaType.Album)
                if not db_id:
                    asyncio.create_task(self.add_album(item, skip_match=True))

    async def match_track(self, searchtrack:Album):
        ''' try to match track in this provider by supplying db track '''
        for prov_mapping in searchtrack.provider_ids:
            if prov_mapping["provider"] == self.prov_id:
                return # we already have a mapping on this provider
        searchstr = "%s - %s" %(searchtrack.artists[0].name, searchtrack.name)
        search_results = await self.search(searchstr, [MediaType.Track], limit=5)
        for item in search_results["tracks"]:
            if item.name == searchtrack.name and item.version == searchtrack.version and item.album.name == searchtrack.album.name:
                # just lazy load this item in the database, it will be matched automagically ;-)
                db_id = await self.mass.db.get_database_id(self.prov_id, item.item_id, MediaType.Track)
                if not db_id:
                    asyncio.create_task(self.add_track(item, skip_match=True))

    ### Provider specific implementation #####

    async def search(self, searchstring, media_types=List[MediaType], limit=5):
        ''' perform search on the provider '''
        raise NotImplementedError
    
    async def get_library_artists(self) -> List[Artist]:
        ''' retrieve library artists from the provider '''
        raise NotImplementedError
    
    async def get_library_albums(self) -> List[Album]:
        ''' retrieve library albums from the provider '''
        raise NotImplementedError

    async def get_library_tracks(self) -> List[Track]:
        ''' retrieve library tracks from the provider '''
        raise NotImplementedError

    async def get_library_playlists(self) -> List[Playlist]:
        ''' retrieve library/subscribed playlists from the provider '''
        raise NotImplementedError

    async def get_artist(self, prov_item_id) -> Artist:
        ''' get full artist details by id '''
        raise NotImplementedError

    async def get_artist_albums(self, prov_item_id) -> List[Album]:
        ''' get a list of albums for the given artist '''
        raise NotImplementedError
    
    async def get_artist_toptracks(self, prov_item_id) -> List[Track]:
        ''' get a list of most popular tracks for the given artist '''
        raise NotImplementedError

    async def get_album(self, prov_item_id) -> Album:
        ''' get full album details by id '''
        raise NotImplementedError

    async def get_track(self, prov_item_id) -> Track:
        ''' get full track details by id '''
        raise NotImplementedError

    async def get_playlist(self, prov_item_id) -> Playlist:
        ''' get full playlist details by id '''
        raise NotImplementedError

    async def get_album_tracks(self, prov_item_id, limit=100, offset=0) -> List[Track]:
        ''' get album tracks for given album id '''
        raise NotImplementedError

    async def get_playlist_tracks(self, prov_item_id, limit=100, offset=0) -> List[Track]:
        ''' get playlist tracks for given playlist id '''
        raise NotImplementedError

    async def add_library(self, prov_item_id, media_type:MediaType):
        ''' add item to library '''
        raise NotImplementedError

    async def remove_library(self, prov_item_id, media_type:MediaType):
        ''' remove item from library '''
        raise NotImplementedError
    
class PlayerState(str, Enum):
    Off = "off"
    Stopped = "stopped"
    Paused = "paused"
    Playing = "playing"

class MusicPlayer():
    ''' representation of a musicplayer '''
    def __init__(self):
        self.player_id = None
        self.player_provider = None
        self.name = ''
        self.state = PlayerState.Off
        self.powered = False
        self.cur_item = Track()
        self.cur_item_time = 0
        self.volume_level = 0
        self.shuffle_enabled = False
        self.repeat_enabled = False
        self.muted = False
        self.group_parent = None # set to id of REAL group/parent player
        self.is_group = False # is this player a group player ?
        self.disable_volume = False
        self.mute_as_power = False
        self.apply_group_volume = False
        self.enabled = False

class PlayerProvider():
    ''' 
        Model for a Playerprovider
        Common methods usable for every provider
        Provider specific __get methods shoud be overriden in the provider specific implementation
    '''
    name = 'My great Musicplayer provider' # display name
    prov_id = 'my_provider' # used as id
    icon = ''
    supports_queue = True # whether this provider has native support for a queue
    supports_http_stream = True # whether we can fallback to http streaming
    supported_musicproviders = [ # list with tuples of supported provider_id and media_types this playerprovider supports NATIVELY, order by preference/quality
            ('qobuz', [MediaType.Track]), 
            ('file', [MediaType.Track, MediaType.Artist, MediaType.Album, MediaType.Playlist]),
            ('spotify', [MediaType.Track, MediaType.Artist, MediaType.Album, MediaType.Playlist])
        ]
    
    def __init__(self, mass):
        self.mass = mass

    ### Common methods and properties ####


    async def play_media(self, player_id, uri, queue_opt='play'):
        ''' 
            play media on a player
            params:
            - player_id: id of the player
            - uri: the uri for/to the media item (e.g. spotify:track:1234 or http://pathtostream)
            - queue_opt: 
                replace: replace whatever is currently playing with this media
                next: the given media will be played after the currently playing track
                add: add to the end of the queue
                play: keep existing queue but play the given item now
        '''
        raise NotImplementedError


    ### Provider specific implementation #####

    async def player_command(self, player_id, cmd:str, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume) '''
        raise NotImplementedError

    async def player_queue(self, player_id, offset=0, limit=50):
        ''' return the items in the player's queue '''
        raise NotImplementedError


