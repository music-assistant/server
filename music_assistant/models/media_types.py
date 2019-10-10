#!/usr/bin/env python3
# -*- coding:utf-8 -*-

from enum import Enum, IntEnum

class MediaType(IntEnum):
    Artist = 1
    Album = 2
    Track = 3
    Playlist = 4
    Radio = 5

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
    elif 'radio' in media_type_str or media_type_str == '5':
        return MediaType.Radio
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
    FLAC_LOSSLESS_HI_RES_1 = 7 # 44.1/48khz 24 bits HI-RES
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
        self.uri = ""
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

class Radio(Track):
    ''' representation of a radio station '''
    def __init__(self):
        super().__init__()
        self.item_id = None
        self.provider = 'database'
        self.name = ''
        self.provider_ids = []
        self.metadata = {}
        self.media_type = MediaType.Radio
        self.in_library = []
        self.is_editable = False
        self.duration = 0


