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

class MediaItem(object):
    ''' representation of a media item '''
    def __init__(self):
        self.item_id = None
        self.provider = 'database'
        self.name = ''
        self.metadata = {}
        self.tags = []
        self.external_ids = []
        self.provider_ids = []
        self.in_library = []
        self.is_lazy = False
        self.available = True
    def __eq__(self, other): 
        if not isinstance(other, self.__class__):
            return NotImplemented
        return (self.name == other.name and 
                self.item_id == other.item_id and
                self.provider == other.provider)
    def __ne__(self, other):
        return not self.__eq__(other)

class Artist(MediaItem):
    ''' representation of an artist '''
    def __init__(self):
        super().__init__()
        self.sort_name = ''
        self.media_type = MediaType.Artist

class Album(MediaItem):
    ''' representation of an album '''
    def __init__(self):
        super().__init__()
        self.version = ''
        self.albumtype = AlbumType.Album
        self.year = 0
        self.artist = None
        self.labels = []
        self.media_type = MediaType.Album

class Track(MediaItem):
    ''' representation of a track '''
    def __init__(self):
        super().__init__()
        self.duration = 0
        self.version = ''
        self.artists = []
        self.album = None
        self.disc_number = 1
        self.track_number = 1
        self.media_type = MediaType.Track

class Playlist(MediaItem):
    ''' representation of a playlist '''
    def __init__(self):
        super().__init__()
        self.owner = ''
        self.media_type = MediaType.Playlist
        self.is_editable = False
        self.checksum = '' # some value to detect playlist track changes

class Radio(MediaItem):
    ''' representation of a radio station '''
    def __init__(self):
        super().__init__()
        self.media_type = MediaType.Radio
        self.duration = 86400


