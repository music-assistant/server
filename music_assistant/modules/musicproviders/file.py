#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
import sys
import time
from utils import run_periodic, LOGGER, parse_track_title
from models import MusicProvider, MediaType, TrackQuality, AlbumType, Artist, Album, Track, Playlist
from constants import CONF_ENABLED
import taglib
from modules.cache import use_cache


def setup(mass):
    ''' setup the provider'''
    enabled = mass.config["musicproviders"]['file'].get(CONF_ENABLED)
    music_dir = mass.config["musicproviders"]['file'].get('music_dir')
    playlists_dir = mass.config["musicproviders"]['file'].get('playlists_dir')
    if enabled and (music_dir or playlists_dir):
        file_provider = FileProvider(mass, music_dir, playlists_dir)
        return file_provider
    return False

def config_entries():
    ''' get the config entries for this provider (list with key/value pairs)'''
    return [
        (CONF_ENABLED, False, CONF_ENABLED),
        ("music_dir", "", "file_prov_music_path"), 
        ("playlists_dir", "", "file_prov_playlists_path")
        ]

class FileProvider(MusicProvider):
    ''' 
        Very basic implementation of a musicprovider for local files
        Assumes files are stored on disk in format <artist>/<album>/<track.ext>
        Reads ID3 tags from file and falls back to parsing filename
        Supports m3u files only for playlists
        Supports having URI's from streaming providers within m3u playlist
        Should be compatible with LMS
    '''
    

    def __init__(self, mass, music_dir, playlists_dir):
        self.name = 'Local files and playlists'
        self.prov_id = 'file'
        self.mass = mass
        self.cache = mass.cache
        self._music_dir = music_dir
        self._playlists_dir = playlists_dir

    async def search(self, searchstring, media_types=List[MediaType], limit=5):
        ''' perform search on the provider '''
        result = {
            "artists": [],
            "albums": [],
            "tracks": [],
            "playlists": []
        }
        return result
    
    async def get_library_artists(self) -> List[Artist]:
        ''' get artist folders in music directory '''
        if not os.path.isdir(self._music_dir):
            LOGGER.error("music path does not exist: %s" % self._music_dir)
            return []
        result = []
        for dirname in os.listdir(self._music_dir):
            dirpath = os.path.join(self._music_dir, dirname)
            if os.path.isdir(dirpath) and not dirpath.startswith('.'):
                artist = await self.get_artist(dirpath)
                if artist:
                    result.append(artist)
        return result
    
    async def get_library_albums(self) -> List[Album]:
        ''' get album folders recursively '''
        result = []
        for artist in await self.get_library_artists():
            result += await self.get_artist_albums(artist.item_id)
        return result

    async def get_library_tracks(self) -> List[Track]:
        ''' get all tracks recursively '''
        #TODO: support disk subfolders
        result = []
        for album in await self.get_library_albums():
            result += await self.get_album_tracks(album.item_id)
        return result
    
    async def get_playlists(self) -> List[Playlist]:
        ''' retrieve playlists from disk '''
        if not self._playlists_dir:
            return []
        result = []
        for filename in os.listdir(self._playlists_dir):
            filepath = os.path.join(self._playlists_dir, filename)
            if os.path.isfile(filepath) and not filename.startswith('.') and filename.lower().endswith('.m3u'):
                playlist = await self.get_playlist(filepath)
                if playlist:
                    result.append(playlist)
        return result 

    async def get_artist(self, prov_item_id) -> Artist:
        ''' get full artist details by id '''
        if not os.path.isdir(prov_item_id):
            LOGGER.error("artist path does not exist: %s" % prov_item_id)
            return None
        if "\\" in prov_item_id:
            name = prov_item_id.split("\\")[-1]
        else:
            name = prov_item_id.split("/")[-1]
        artist = Artist()
        artist.item_id = prov_item_id
        artist.provider = self.prov_id
        artist.name = name
        artist.provider_ids.append({
            "provider": self.prov_id,
            "item_id": prov_item_id
        })
        return artist
        
    async def get_album(self, prov_item_id) -> Album:
        ''' get full album details by id '''
        if not os.path.isdir(prov_item_id):
            LOGGER.error("album path does not exist: %s" % prov_item_id)
            return None
        if "\\" in prov_item_id:
            name = prov_item_id.split("\\")[-1]
            artistpath = prov_item_id.rsplit("\\", 1)[0]
        else:
            name = prov_item_id.split("/")[-1]
            artistpath = prov_item_id.rsplit("/", 1)[0]
        album = Album()
        album.item_id = prov_item_id
        album.provider = self.prov_id
        album.name, album.version = parse_track_title(name)
        album.artist = await self.get_artist(artistpath)
        if not album.artist:
            raise Exception("No album artist ! %s" % artistpath)
        album.provider_ids.append({
            "provider": self.prov_id,
            "item_id": prov_item_id
        })
        return album

    async def get_track(self, prov_item_id) -> Track:
        ''' get full track details by id '''
        if not os.path.isfile(prov_item_id):
            LOGGER.error("track path does not exist: %s" % prov_item_id)
            return None
        return await self.__parse_track(prov_item_id)

    async def get_playlist(self, prov_item_id) -> Playlist:
        ''' get full playlist details by id '''
        if not os.path.isfile(prov_item_id):
            LOGGER.error("playlist path does not exist: %s" % prov_item_id)
            return None
        filepath = prov_item_id
        playlist = Playlist()
        playlist.item_id = filepath
        playlist.provider = self.prov_id
        playlist.name = filepath.split('\\')[-1].split('/')[-1].replace('.m3u', '')
        playlist.is_editable = True
        playlist.provider_ids.append({
            "provider": self.prov_id,
            "item_id": filepath
        })
        playlist.owner = 'disk'
        return playlist
    
    async def get_album_tracks(self, prov_album_id) -> List[Track]:
        ''' get album tracks for given album id '''
        result = []
        albumpath = prov_album_id
        if not os.path.isdir(albumpath):
            LOGGER.error("album path does not exist: %s" % albumpath)
            return []
        album = await self.get_album(albumpath)
        for filename in os.listdir(albumpath):
            filepath = os.path.join(albumpath, filename)
            if os.path.isfile(filepath) and not filepath.startswith('.'):
                track = await self.__parse_track(filepath)
                if track:
                    track.album = album
                    result.append(track)
        return result

    async def get_playlist_tracks(self, prov_playlist_id, limit=50, offset=0) -> List[Track]:
        ''' get playlist tracks for given playlist id '''
        tracks = []
        if not os.path.isfile(prov_playlist_id):
            LOGGER.error("playlist path does not exist: %s" % prov_playlist_id)
            return []
        counter = 0
        with open(prov_playlist_id) as f:
            for line in f.readlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    counter += 1
                    if counter > offset:
                        track = await self.__parse_track_from_uri(line)
                        if track:
                            tracks.append(track)
                    if limit and len(tracks) == limit:
                        break
        return tracks

    async def get_artist_albums(self, prov_artist_id) -> List[Album]:
        ''' get a list of albums for the given artist '''
        result = []
        artistpath = prov_artist_id
        if not os.path.isdir(artistpath):
            LOGGER.error("artist path does not exist: %s" % artistpath)
            return []
        for dirname in os.listdir(artistpath):
            dirpath = os.path.join(artistpath, dirname)
            if os.path.isdir(dirpath) and not dirpath.startswith('.'):
                album = await self.get_album(dirpath)
                if album:
                    result.append(album)
        return result

    async def get_artist_toptracks(self, prov_artist_id) -> List[Track]:
        ''' get a list of 10 random tracks as we have no clue about preference '''
        tracks = []
        for album in await self.get_artist_albums(prov_artist_id):
            tracks += await self.get_album_tracks(album.item_id)
        return tracks[:10]

    async def get_stream_details(self, track_id):
        ''' returns the stream details for the given track '''
        track = await self.track(track_id)
        import socket
        host = socket.gethostbyname(socket.gethostname())
        return {
            'mime_type': 'audio/flac',
            'duration': track.duration,
            'sampling_rate': 44100,
            'bit_depth': 16,
            'url': 'http://%s/stream/file/%s' % (host, track_id)
        }
    
    async def get_stream(self, track_id):
        ''' get audio stream for a track '''
        with open(track_id) as f:
            while True:
                line = f.readline()
                if line:
                    yield line
                else:
                    break
    
    async def __parse_track(self, filename):
        ''' try to parse a track from a filename with taglib '''
        track = Track()
        try:
            song = taglib.File(filename)
        except:
            return None # not a media file ?
        track.duration = song.length
        track.item_id = filename
        track.provider = self.prov_id
        name = song.tags['TITLE'][0]
        track.name, track.version = parse_track_title(name)
        if "\\" in filename:
            albumpath = filename.rsplit("\\",1)[0]
        else:
            albumpath = filename.rsplit("/",1)[0]
        track.album = await self.get_album(albumpath)
        artists = []
        for artist_str in song.tags['ARTIST']:
            local_artist_path = os.path.join(self._music_dir, artist_str)
            if os.path.isfile(local_artist_path):
                artist = await self.get_artist(local_artist_path)
            else:
                artist = Artist()
                artist.name = artist_str
                fake_artistpath = os.path.join(self._music_dir, artist_str)
                artist.item_id = fake_artistpath # temporary id
                artist.provider_ids.append({
                        "provider": self.prov_id,
                        "item_id": fake_artistpath
                    })
            artists.append(artist)
        track.artists = artists
        if 'GENRE' in song.tags:
            track.tags = song.tags['GENRE']
        if 'ISRC' in song.tags:
            track.external_ids.append( {"isrc": song.tags['ISRC'][0]} ) 
        if 'DISCNUMBER' in song.tags:
            track.disc_number = int(song.tags['DISCNUMBER'][0])
        if 'TRACKNUMBER' in song.tags:
            track.track_number = int(song.tags['TRACKNUMBER'][0])
        quality_details = ""
        if filename.endswith('.flac'):
            # TODO: get bit depth
            quality = TrackQuality.FLAC_LOSSLESS
            if song.sampleRate > 192000:
                quality = TrackQuality.FLAC_LOSSLESS_HI_RES_4
            elif song.sampleRate > 96000:
                quality = TrackQuality.FLAC_LOSSLESS_HI_RES_3
            elif song.sampleRate > 48000:
                quality = TrackQuality.FLAC_LOSSLESS_HI_RES_2
            quality_details = "%s Khz" % (song.sampleRate/1000)
        elif filename.endswith('.ogg'):
            quality = TrackQuality.LOSSY_OGG
            quality_details = "%s kbps" % (song.bitrate)
        elif filename.endswith('.m4a'):
            quality = TrackQuality.LOSSY_AAC
            quality_details = "%s kbps" % (song.bitrate)
        else:
            quality = TrackQuality.LOSSY_MP3
            quality_details = "%s kbps" % (song.bitrate)
        track.provider_ids.append({
            "provider": self.prov_id,
            "item_id": filename,
            "quality": quality,
            "details": quality_details
        })
        return track
                
    async def __parse_track_from_uri(self, uri):
        ''' try to parse a track from an uri found in playlist '''
        if "://" in uri:
            # track is uri from external provider?
            prov_id = uri.split('://')[0]
            prov_item_id = uri.split('/')[-1].split('.')[0].split(':')[-1]
            try:
                return await self.mass.music.providers[prov_id].track(prov_item_id, lazy=False)
            except Exception as exc:
                LOGGER.warning("Could not parse uri %s to track: %s" %(uri, str(exc)))
                return None
        # try to treat uri as filename
        # TODO: filename could be related to musicdir or full path
        track = await self.get_track(uri)
        if track:
            return track
        track = await self.get_track(os.path.join(self._music_dir, uri))
        if track:
            return track
        return None
