#!/usr/bin/env python3
# -*- coding:utf-8 -*-

from enum import Enum, IntEnum
from typing import List
import sys
sys.path.append("..")
from utils import run_periodic, LOGGER, parse_track_title
from difflib import SequenceMatcher as Matcher
import asyncio
from modules.cache import use_cache


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
        self.provider = 'database'
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
        self.provider = 'database'
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
        self.provider = 'database'
        self.name = ''
        self.duration = 0
        self.version = ''
        self.external_ids = []
        self.metadata = { }
        self.tags = []
        self.artists = []
        self.provider_ids = []
        self.album = None
        self.disc_number = 1
        self.track_number = 1
        self.media_type = MediaType.Track
        self.in_library = []
        self.is_lazy = False
    def __eq__(self, other): 
        if not isinstance(other, self.__class__):
            return NotImplemented
        return (self.name == other.name and 
                self.version == other.version and
                self.item_id == other.item_id and
                self.provider == other.provider)
    def __ne__(self, other):
        return not self.__eq__(other)

class Playlist(object):
    ''' representation of a playlist '''
    def __init__(self):
        self.item_id = None
        self.provider = 'database'
        self.name = ''
        self.owner = ''
        self.provider_ids = []
        self.metadata = {}
        self.media_type = MediaType.Playlist
        self.in_library = []
        self.is_editable = False

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
                raise Exception('artist not found: %s' % prov_item_id)
            if lazy:
                asyncio.create_task(self.add_artist(artist_details))
                artist_details.is_lazy = True
                return artist_details
            item_id = await self.add_artist(artist_details)
        return await self.mass.db.artist(item_id)

    async def add_artist(self, artist_details) -> int:
        ''' add artist to local db and return the new database id'''
        musicbrainz_id = None
        for item in artist_details.external_ids:
            if item.get("musicbrainz"):
                musicbrainz_id = item["musicbrainz"]
        if not musicbrainz_id:
            musicbrainz_id = await self.get_artist_musicbrainz_id(artist_details)
        if not musicbrainz_id:
            return
        # grab additional metadata
        if musicbrainz_id:
            artist_details.external_ids.append({"musicbrainz": musicbrainz_id})
            artist_details.metadata = await self.mass.metadata.get_artist_metadata(musicbrainz_id, artist_details.metadata)
        item_id = await self.mass.db.add_artist(artist_details)
        # also fetch same artist on all providers
        new_artist = await self.mass.db.artist(item_id)
        new_artist_toptracks = await self.get_artist_toptracks(artist_details.item_id)
        if new_artist_toptracks:
            item_provider_keys = [item['provider'] for item in new_artist.provider_ids]
            for prov_id, provider in self.mass.music.providers.items():
                if not prov_id in item_provider_keys:
                    await provider.match_artist(new_artist, new_artist_toptracks)
        return item_id

    async def get_artist_musicbrainz_id(self, artist_details:Artist):
        ''' fetch musicbrainz id by performing search with both the artist and one of it's albums or tracks '''
        musicbrainz_id = ""
        # try with album first
        lookup_albums = await self.get_artist_albums(artist_details.item_id)
        for lookup_album in lookup_albums[:5]:
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
        if not musicbrainz_id:
            lookup_tracks = await self.get_artist_toptracks(artist_details.item_id)
            for lookup_track in lookup_tracks:
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
            musicbrainz_id = artist_details.name
        return musicbrainz_id

    async def album(self, prov_item_id, lazy=True) -> Album:
        ''' return album details for the given provider album id'''
        item_id = await self.mass.db.get_database_id(self.prov_id, prov_item_id, MediaType.Album)
        if not item_id:
            # album not yet in local database so fetch details
            album_details = await self.get_album(prov_item_id)
            if not album_details:
                raise Exception('album not found: %s' % prov_item_id)
            if lazy:
                asyncio.create_task(self.add_album(album_details))
                album_details.is_lazy = True
                return album_details
            item_id = await self.add_album(album_details)
        return await self.mass.db.album(item_id)

    async def add_album(self, album_details) -> int:
        ''' add album to local db and return the new database id'''
        # we need to fetch album artist too
        db_album_artist = await self.artist(album_details.artist.item_id, lazy=False)
        album_details.artist = db_album_artist
        item_id = await self.mass.db.add_album(album_details)
        # also fetch same album on all providers
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
                raise Exception('track not found: %s' % prov_item_id)
            if lazy:
                asyncio.create_task(self.add_track(track_details))
                track_details.is_lazy = True
                return track_details
            item_id = await self.add_track(track_details)
        return await self.mass.db.track(item_id)

    async def add_track(self, track_details, prov_album_id=None) -> int:
        ''' add track to local db and return the new database id'''
        track_artists = []
        # we need to fetch track artists too
        for track_artist in track_details.artists:
            db_track_artist = await self.artist(track_artist.item_id, lazy=False)
            if db_track_artist:
                track_artists.append(db_track_artist)
        track_details.artists = track_artists
        if not prov_album_id:
            prov_album_id = track_details.album.item_id
        track_details.album = await self.album(prov_album_id, lazy=False)
        item_id = await self.mass.db.add_track(track_details)
        # also fetch same track on all providers (will also get other quality versions)
        new_track = await self.mass.db.track(item_id)
        for prov_id, provider in self.mass.music.providers.items():
            await provider.match_track(new_track)
        return item_id
    
    async def playlist(self, prov_playlist_id) -> Playlist:
        ''' return playlist details for the given provider playlist id '''
        db_id = await self.mass.db.get_database_id(self.prov_id, prov_playlist_id, MediaType.Playlist)
        if db_id:
            # synced playlist, return database details
            return await self.mass.db.playlist(db_id)
        else:
            return await self.get_playlist(prov_playlist_id)

    async def album_tracks(self, prov_album_id) -> List[Track]:
        ''' return album tracks for the given provider album id'''
        items = []
        album = await self.get_album(prov_album_id)
        for prov_track in await self.get_album_tracks(prov_album_id):
            db_id = await self.mass.db.get_database_id(self.prov_id, prov_track.item_id, MediaType.Track) 
            if db_id:
                items.append( await self.mass.db.track(db_id) )
            else:
                prov_track.album = album
                items.append(prov_track)
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
        return items
    
    async def match_artist(self, searchartist:Artist, searchtracks:List[Track]):
        ''' try to match artist in this provider by supplying db artist '''
        for searchtrack in searchtracks:
            searchstr = "%s - %s" %(searchartist.name, searchtrack.name)
            search_results = await self.search(searchstr, [MediaType.Track], limit=5)
            for item in search_results["tracks"]:
                if item.name == searchtrack.name and item.version == searchtrack.version and item.album.name == searchtrack.album.name:
                    # double safety check - artist must match exactly !
                    for artist in item.artists:
                        if artist.name == searchartist.name:
                            # just load this item in the database, it will be matched automagically ;-)
                            return await self.artist(artist.item_id, lazy=False)

    async def match_album(self, searchalbum:Album):
        ''' try to match album in this provider by supplying db album '''
        searchstr = "%s - %s %s" %(searchalbum.artist.name, searchalbum.name, searchalbum.version)
        search_results = await self.search(searchstr, [MediaType.Album], limit=5)
        for item in search_results["albums"]:
            if item.name == searchalbum.name and item.version == searchalbum.version and item.artist.name == searchalbum.artist.name:
                # just load this item in the database, it will be matched automagically ;-)
                await self.album(item.item_id, lazy=False)

    async def match_track(self, searchtrack:Track):
        ''' try to match track in this provider by supplying db track '''
        searchstr = "%s - %s" %(searchtrack.artists[0].name, searchtrack.name)
        searchartists = [item.name for item in searchtrack.artists]
        search_results = await self.search(searchstr, [MediaType.Track], limit=5)
        for item in search_results["tracks"]:
            if item.name == searchtrack.name and item.version == searchtrack.version and item.album.name == searchtrack.album.name:
                # double safety check - artist must match exactly !
                for artist in item.artists:
                    if artist.name in searchartists:
                        # just load this item in the database, it will be matched automagically ;-)
                        await self.track(item.item_id, lazy=False)

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

    async def get_playlists(self) -> List[Playlist]:
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

    async def add_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        ''' add track(s) to playlist '''
        raise NotImplementedError

    async def remove_playlist_tracks(self, prov_playlist_id, prov_track_ids):
        ''' remove track(s) from playlist '''
        raise NotImplementedError

    async def get_stream_content_type(self, track_id):
        ''' return the content type for the given track when it will be streamed'''
        raise NotImplementedError
    
    async def get_stream(self, track_id):
        ''' get audio stream for a track '''
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
        self.state = PlayerState.Stopped
        self.powered = False
        self.cur_item = Track()
        self.cur_item_time = 0
        self.volume_level = 0
        self.shuffle_enabled = True
        self.repeat_enabled = False
        self.muted = False
        self.group_parent = None # set to id of REAL group/parent player
        self.is_group = False # is this player a group player ?
        self.settings = {}
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
    supported_musicproviders = ['qobuz', 'file', 'spotify', 'http'] # list of supported music provider uri's this playerprovider supports NATIVELY
    
    def __init__(self, mass):
        self.mass = mass

    ### Common methods and properties ####


    async def play_media(self, player_id, media_items:List[Track], queue_opt='play'):
        ''' 
            play media on a player
            params:
            - player_id: id of the player
            - media_items: List of Tracks to play, each Track will contain uri attribute (e.g. spotify:track:1234 or http://pathtostream)
            - queue_opt: 
                replace: replace whatever is currently playing with this media
                next: the given media will be played after the currently playing track
                add: add to the end of the queue
                play: keep existing queue but play the given item(s) now first
        '''
        raise NotImplementedError


    ### Provider specific implementation #####

    async def player_command(self, player_id, cmd:str, cmd_args=None):
        ''' issue command on player (play, pause, next, previous, stop, power, volume) '''
        raise NotImplementedError

    async def player_queue(self, player_id, offset=0, limit=50):
        ''' return the items in the player's queue '''
        raise NotImplementedError


