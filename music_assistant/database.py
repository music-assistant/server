#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import os
from typing import List
import aiosqlite
import operator
import logging

from .utils import run_periodic, LOGGER, get_sort_name, try_parse_int
from .models.media_types import MediaType, Artist, Album, Track, Playlist, Radio

class Database():

    def __init__(self, datapath):
        if not os.path.isdir(datapath):
            raise FileNotFoundError(f"data directory {datapath} does not exist!")
        self.dbfile = os.path.join(datapath, "database.db")
        logging.getLogger('aiosqlite').setLevel(logging.INFO)

    async def setup(self):
        ''' init database '''
        async with aiosqlite.connect(self.dbfile) as db:

            await db.execute('CREATE TABLE IF NOT EXISTS library_items(item_id INTEGER NOT NULL, provider TEXT NOT NULL, media_type INTEGER NOT NULL, UNIQUE(item_id, provider, media_type));')

            await db.execute('CREATE TABLE IF NOT EXISTS artists(artist_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort_name TEXT, musicbrainz_id TEXT NOT NULL UNIQUE);')            
            await db.execute('CREATE TABLE IF NOT EXISTS albums(album_id INTEGER PRIMARY KEY AUTOINCREMENT, artist_id INTEGER NOT NULL, name TEXT NOT NULL, albumtype TEXT, year INTEGER, version TEXT, UNIQUE(artist_id, name, version, albumtype));')
            await db.execute('CREATE TABLE IF NOT EXISTS labels(label_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);')
            await db.execute('CREATE TABLE IF NOT EXISTS album_labels(album_id INTEGER, label_id INTEGER, UNIQUE(album_id, label_id));')

            await db.execute('CREATE TABLE IF NOT EXISTS tracks(track_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, album_id INTEGER, duration INTEGER, version TEXT, disc_number INT, track_number INT, UNIQUE(name, album_id, version));')
            await db.execute('CREATE TABLE IF NOT EXISTS track_artists(track_id INTEGER, artist_id INTEGER, UNIQUE(track_id, artist_id));')
            
            await db.execute('CREATE TABLE IF NOT EXISTS tags(tag_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);')
            await db.execute('CREATE TABLE IF NOT EXISTS media_tags(item_id INTEGER, media_type INTEGER, tag_id, UNIQUE(item_id, media_type, tag_id));')
            
            await db.execute('CREATE TABLE IF NOT EXISTS provider_mappings(item_id INTEGER NOT NULL, media_type INTEGER NOT NULL, prov_item_id TEXT NOT NULL, provider TEXT NOT NULL, quality INTEGER NOT NULL, details TEXT NULL, UNIQUE(item_id, media_type, prov_item_id, provider, quality));')
            
            await db.execute('CREATE TABLE IF NOT EXISTS metadata(item_id INTEGER NOT NULL, media_type INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, UNIQUE(item_id, media_type, key));')
            await db.execute('CREATE TABLE IF NOT EXISTS external_ids(item_id INTEGER NOT NULL, media_type INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, UNIQUE(item_id, media_type, key, value));')
            
            await db.execute('CREATE TABLE IF NOT EXISTS playlists(playlist_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, owner TEXT NOT NULL, is_editable BOOLEAN NOT NULL, UNIQUE(name, owner));')
            await db.execute('CREATE TABLE IF NOT EXISTS playlist_tracks(playlist_id INTEGER NOT NULL, track_id INTEGER NOT NULL, position INTEGER, UNIQUE(playlist_id, track_id));')
            
            await db.execute('CREATE TABLE IF NOT EXISTS radios(radio_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);')
            
            await db.execute('CREATE TABLE IF NOT EXISTS track_loudness(provider_track_id INTEGER NOT NULL, provider TEXT NOT NULL, loudness REAL, UNIQUE(provider_track_id, provider));')
            
            await db.commit()
            await db.execute('VACUUM;')

    async def get_database_id(self, provider:str, prov_item_id:str, media_type:MediaType):
        ''' get the database id for the given prov_id '''
        async with aiosqlite.connect(self.dbfile) as db:
            sql_query = 'SELECT item_id FROM provider_mappings WHERE prov_item_id = ? AND provider = ? AND media_type = ?;'
            cursor = await db.execute(sql_query, (prov_item_id, provider, media_type))
            item_id = await cursor.fetchone()
            if item_id:
                item_id = item_id[0]
            await cursor.close()
            return item_id
    
    async def search(self, searchquery, media_types:List[MediaType], limit=10):
        ''' search library for the given searchphrase '''
        result = {
            "artists": [],
            "albums": [],
            "tracks": [],
            "playlists": []
        }
        searchquery = "%" + searchquery + "%"
        sql_query = ' WHERE name LIKE "%s"' % searchquery
        if MediaType.Artist in media_types:
            result["artists"] = await self.artists(sql_query, limit=limit)
        if MediaType.Album in media_types:
            result["albums"] = await self.albums(sql_query, limit=limit)
        if MediaType.Track in media_types:
            sql_query = 'SELECT * FROM tracks WHERE name LIKE "%s"' % searchquery
            result["tracks"] = await self.tracks(sql_query, limit=limit)
        if MediaType.Playlist in media_types:
            result["playlists"] = await self.playlists(sql_query, limit=limit)
        return result
    
    async def library_artists(self, provider=None, limit=100000, offset=0, orderby='name') -> List[Artist]:
        ''' get all library artists, optionally filtered by provider'''
        if provider != None:
            sql_query = ' WHERE artist_id in (SELECT item_id FROM library_items WHERE provider = "%s" AND media_type = %d)' % (provider,MediaType.Artist)
        else:
            sql_query = ' WHERE artist_id in (SELECT item_id FROM library_items WHERE media_type = %d)' % MediaType.Artist
        return await self.artists(sql_query, limit=limit, offset=offset, orderby=orderby)

    async def library_albums(self, provider=None, limit=100000, offset=0, orderby='name') -> List[Album]:
        ''' get all library albums, optionally filtered by provider'''
        if provider != None:
            sql_query = ' WHERE album_id in (SELECT item_id FROM library_items WHERE provider = "%s" AND media_type = %d)' % (provider,MediaType.Album)
        else:
            sql_query = ' WHERE album_id in (SELECT item_id FROM library_items WHERE media_type = %d)' % MediaType.Album
        return await self.albums(sql_query, limit=limit, offset=offset, orderby=orderby)

    async def library_tracks(self, provider=None, limit=100000, offset=0, orderby='name') -> List[Track]:
        ''' get all library tracks, optionally filtered by provider'''
        if provider != None:
            sql_query = 'SELECT * FROM tracks WHERE track_id in (SELECT item_id FROM library_items WHERE provider = "%s" AND media_type = %d)' % (provider,MediaType.Track)
        else:
            sql_query = 'SELECT * FROM tracks WHERE track_id in (SELECT item_id FROM library_items WHERE media_type = %d)' % MediaType.Track
        return await self.tracks(sql_query, limit=limit, offset=offset, orderby=orderby)
    
    async def playlists(self, filter_query=None, provider=None, limit=100000, offset=0, orderby='name') -> List[Playlist]:
        ''' fetch all playlist records from table'''
        playlists = []
        sql_query = 'SELECT * FROM playlists'
        if filter_query:
            sql_query += filter_query
        elif provider != None:
            sql_query += ' WHERE playlist_id in (SELECT item_id FROM provider_mappings WHERE provider = "%s" AND media_type = %d)' % (provider,MediaType.Playlist)
        sql_query += ' ORDER BY %s' % orderby
        if limit:
            sql_query += ' LIMIT %d OFFSET %d' %(limit, offset)
        async with aiosqlite.connect(self.dbfile) as db:
            async with db.execute(sql_query) as cursor:
                db_rows = await cursor.fetchall()
            for db_row in db_rows:
                playlist = Playlist()
                playlist.item_id = db_row[0]
                playlist.name = db_row[1]
                playlist.owner = db_row[2]
                playlist.is_editable = db_row[3]
                playlist.metadata = await self.__get_metadata(playlist.item_id, MediaType.Playlist, db)
                playlist.provider_ids = await self.__get_prov_ids(playlist.item_id, MediaType.Playlist, db)
                playlist.in_library = await self.__get_library_providers(playlist.item_id, MediaType.Playlist, db)
                playlists.append(playlist)
        return playlists

    async def radios(self, filter_query=None, provider=None, limit=100000, offset=0, orderby='name') -> List[Radio]:
        ''' fetch all radio records from table'''
        items = []
        sql_query = 'SELECT * FROM radios'
        if filter_query:
            sql_query += filter_query
        elif provider != None:
            sql_query += ' WHERE radio_id in (SELECT item_id FROM provider_mappings WHERE provider = "%s" AND media_type = %d)' % (provider,MediaType.Radio)
        sql_query += ' ORDER BY %s' % orderby
        if limit:
            sql_query += ' LIMIT %d OFFSET %d' %(limit, offset)
        async with aiosqlite.connect(self.dbfile) as db:
            async with db.execute(sql_query) as cursor:
                db_rows = await cursor.fetchall()
            for db_row in db_rows:
                radio = Radio()
                radio.item_id = db_row[0]
                radio.name = db_row[1]
                radio.metadata = await self.__get_metadata(radio.item_id, MediaType.Radio, db)
                radio.provider_ids = await self.__get_prov_ids(radio.item_id, MediaType.Radio, db)
                radio.in_library = await self.__get_library_providers(radio.item_id, MediaType.Radio, db)
                items.append(radio)
        return items

    async def playlist(self, playlist_id:int) -> Playlist:
        ''' get playlist record by id '''
        playlist_id = try_parse_int(playlist_id)
        playlists = await self.playlists(' WHERE playlist_id = %s' % playlist_id)
        if not playlists:
            return None
        return playlists[0]

    async def radio(self, radio_id:int) -> Playlist:
        ''' get radio record by id '''
        radio_id = try_parse_int(radio_id)
        radios = await self.radios(' WHERE radio_id = %s' % radio_id)
        if not radios:
            return None
        return radios[0]

    async def add_playlist(self, playlist:Playlist):
        ''' add a new playlist record into table'''
        assert(playlist.name)
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            async with db.execute('SELECT (playlist_id) FROM playlists WHERE name=? AND owner=?;', (playlist.name, playlist.owner)) as cursor:
                result = await cursor.fetchone()
                if result:
                    playlist_id = result[0]
                    # update existing
                    sql_query = 'UPDATE playlists SET is_editable=? WHERE playlist_id=?;'
                    await db.execute(sql_query, (playlist.is_editable, playlist_id))
                else:
                    # insert playlist
                    sql_query = 'INSERT OR REPLACE INTO playlists (name, owner, is_editable) VALUES(?,?,?);'
                    await db.execute(sql_query, (playlist.name, playlist.owner, playlist.is_editable))
                    # get id from newly created item (the safe way)
                    async with db.execute('SELECT (playlist_id) FROM playlists WHERE name=? AND owner=?;', (playlist.name,playlist.owner)) as cursor:
                        playlist_id = await cursor.fetchone()
                        playlist_id = playlist_id[0]
                    LOGGER.debug('added playlist %s to database: %s' %(playlist.name, playlist_id))
            # add/update metadata
            await self.__add_prov_ids(playlist_id, MediaType.Playlist, playlist.provider_ids, db)
            await self.__add_metadata(playlist_id, MediaType.Playlist, playlist.metadata, db)
            # save
            await db.commit()
        return playlist_id

    async def add_radio(self, radio:Radio):
        ''' add a new radio record into table'''
        assert(radio.name)
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            async with db.execute('SELECT (radio_id) FROM radios WHERE name=?;', (radio.name,)) as cursor:
                result = await cursor.fetchone()
                if result:
                    radio_id = result[0]
                else:
                    # insert radio
                    sql_query = 'INSERT OR REPLACE INTO radios (name) VALUES(?);'
                    await db.execute(sql_query, (radio.name,))
                    # get id from newly created item (the safe way)
                    async with db.execute('SELECT (radio_id) FROM radios WHERE name=?;', (radio.name,)) as cursor:
                        radio_id = await cursor.fetchone()
                        radio_id = radio_id[0]
                    LOGGER.debug('added radio station %s to database: %s' %(radio.name, radio_id))
            # add/update metadata
            await self.__add_prov_ids(radio_id, MediaType.Radio, radio.provider_ids, db)
            await self.__add_metadata(radio_id, MediaType.Radio, radio.metadata, db)
            # save
            await db.commit()
        return radio_id

    async def add_to_library(self, item_id:int, media_type:MediaType, provider:str):
        ''' add an item to the library (item must already be present in the db!) '''
        item_id = try_parse_int(item_id)
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            sql_query = 'INSERT or REPLACE INTO library_items (item_id, provider, media_type) VALUES(?,?,?);'
            await db.execute(sql_query, (item_id, provider, media_type))
            await db.commit()

    async def remove_from_library(self, item_id:int, media_type:MediaType, provider:str):
        ''' remove item from the library '''
        item_id = try_parse_int(item_id)
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            sql_query = 'DELETE FROM library_items WHERE item_id=? AND provider=? AND media_type=?;'
            await db.execute(sql_query, (item_id, provider, media_type))
            if media_type == MediaType.Playlist:
                sql_query = 'DELETE FROM playlist_tracks WHERE playlist_id=?;'
                await db.execute(sql_query, (item_id,))
                sql_query = 'DELETE FROM playlists WHERE playlist_id=?;'
                await db.execute(sql_query, (item_id,))
                sql_query = 'DELETE FROM provider_mappings WHERE item_id=? AND media_type=? AND provider=?;'
                await db.execute(sql_query, (item_id,media_type, provider))
            await db.commit()
    
    async def artists(self, filter_query=None, limit=100000, offset=0, orderby='name', fulldata=True, db=None) -> List[Artist]:
        ''' fetch artist records from table'''
        artists = []
        sql_query = 'SELECT * FROM artists'
        if filter_query:
            sql_query += ' ' + filter_query
        sql_query += ' ORDER BY %s' % orderby
        if limit:
            sql_query += ' LIMIT %d OFFSET %d' %(limit, offset)
        if not db:
            db = await aiosqlite.connect(self.dbfile)
            should_close_db = True
        else:
            should_close_db = False
        async with db.execute(sql_query) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            artist = Artist()
            artist.item_id = db_row[0]
            artist.name = db_row[1]
            artist.sort_name = db_row[2]
            artist.provider_ids = await self.__get_prov_ids(artist.item_id, MediaType.Artist, db)
            artist.in_library = await self.__get_library_providers(artist.item_id, MediaType.Artist, db)
            if fulldata:
                artist.external_ids = await self.__get_external_ids(artist.item_id, MediaType.Artist, db)
                artist.metadata = await self.__get_metadata(artist.item_id, MediaType.Artist, db)
                artist.tags = await self.__get_tags(artist.item_id, MediaType.Artist, db)
                artist.metadata = await self.__get_metadata(artist.item_id, MediaType.Artist, db, filter_key='image')
            artists.append(artist)
        if should_close_db:
            await db.close()
        return artists

    async def artist(self, artist_id:int, fulldata=True) -> Artist:
        ''' get artist record by id '''
        artist_id = try_parse_int(artist_id)
        artists = await self.artists('WHERE artist_id = %s' % artist_id, fulldata=fulldata)
        if not artists:
            return None
        return artists[0]

    async def add_artist(self, artist:Artist):
        ''' add a new artist record into table'''
        artist_id = None
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            # always prefer to grab existing artist with external_id (=musicbrainz_id)
            artist_id = await self.__get_item_by_external_id(artist, db)
            if not artist_id:
                # insert artist
                musicbrainz_id = None
                for item in artist.external_ids:
                    if item.get('musicbrainz'):
                        musicbrainz_id = item['musicbrainz']
                        break
                assert(musicbrainz_id) # musicbrainz id is required
                if not artist.sort_name:
                    artist.sort_name = get_sort_name(artist.name)
                sql_query = 'INSERT OR IGNORE INTO artists (name, sort_name, musicbrainz_id) VALUES(?,?,?);'
                await db.execute(sql_query, (artist.name, artist.sort_name, musicbrainz_id))
                await db.commit()
                # get id from (newly created) item (the safe way)
                artist_id = await self.__get_item_by_external_id(artist, db)
                if not artist_id:
                    async with db.execute('SELECT (artist_id) FROM artists WHERE musicbrainz_id=?;', (musicbrainz_id,)) as cursor:
                        artist_id = await cursor.fetchone()
                        artist_id = artist_id[0]
            # add metadata and tags etc.
            await self.__add_prov_ids(artist_id, MediaType.Artist, artist.provider_ids, db)
            await self.__add_metadata(artist_id, MediaType.Artist, artist.metadata, db)
            await self.__add_tags(artist_id, MediaType.Artist, artist.tags, db)
            await self.__add_external_ids(artist_id, MediaType.Artist, artist.external_ids, db)
            # save
            await db.commit()
        LOGGER.debug('added artist %s (%s) to database: %s' %(artist.name, artist.provider_ids, artist_id))
        return artist_id
    
    async def albums(self, filter_query=None, limit=100000, offset=0, orderby='name', fulldata=True, db=None) -> List[Album]:
        ''' fetch all album records from table'''
        albums = []
        sql_query = 'SELECT * FROM albums'
        if filter_query:
            sql_query += ' ' + filter_query
        sql_query += ' ORDER BY %s' % orderby
        if limit:
            sql_query += ' LIMIT %d OFFSET %d' %(limit, offset)
        if not db:
            db = await aiosqlite.connect(self.dbfile)
            should_close_db = True
        else:
            should_close_db = False
        async with db.execute(sql_query) as cursor:
            db_rows = await cursor.fetchall()
            for db_row in db_rows:
                album = Album()
                album.item_id = db_row[0]
                album.name = db_row[2]
                album.albumtype = db_row[3]
                album.year = db_row[4]
                album.version = db_row[5]
                album.provider_ids = await self.__get_prov_ids(album.item_id, MediaType.Album, db)
                album.in_library = await self.__get_library_providers(album.item_id, MediaType.Album, db)
                if fulldata:
                    album.artist = await self.artist(db_row[1], fulldata=False)
                    album.external_ids = await self.__get_external_ids(album.item_id, MediaType.Album, db)
                    album.metadata = await self.__get_metadata(album.item_id, MediaType.Album, db)
                    album.tags = await self.__get_tags(album.item_id, MediaType.Album, db)
                    album.labels = await self.__get_album_labels(album.item_id, db)
                else:
                    album.metadata = await self.__get_metadata(album.item_id, MediaType.Album, db, filter_key='image')
                albums.append(album)
        if should_close_db:
            await db.close()
        return albums

    async def album(self, album_id:int, fulldata=True, db=None) -> Album:
        ''' get album record by id '''
        album_id = try_parse_int(album_id)
        albums = await self.albums('WHERE album_id = %s' % album_id, fulldata=fulldata, db=db)
        if not albums:
            return None
        return albums[0]

    async def add_album(self, album:Album):
        ''' add a new album record into table'''
        album_id = None
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            # always try to grab existing album with external_id
            album_id = await self.__get_item_by_external_id(album, db)
            # fallback to matching on artist_id, name and version
            if not album_id:
                async with db.execute('SELECT (album_id) FROM albums WHERE artist_id=? AND name=? AND version=?;', (album.artist.item_id, album.name, album.version)) as cursor:
                    result = await cursor.fetchone()
                    if result:
                        album_id = result[0]
            if not album_id and album.year:
                async with db.execute('SELECT (album_id) FROM albums WHERE year=? AND name=? AND version=?;', (album.year, album.name, album.version)) as cursor:
                    result = await cursor.fetchone()
                    if result:
                        album_id = result[0]
            if not album_id:
                # insert album
                sql_query = 'INSERT OR IGNORE INTO albums (artist_id, name, albumtype, year, version) VALUES(?,?,?,?,?);'
                await db.execute(sql_query, (album.artist.item_id, album.name, album.albumtype, album.year, album.version))
                await db.commit()
                # get id from newly created item
                async with db.execute('SELECT (album_id) FROM albums WHERE artist_id=? AND name=? AND version=?;', (album.artist.item_id, album.name, album.version)) as cursor:
                    album_id = await cursor.fetchone()
                    assert(album_id)
                    album_id = album_id[0]
            # add metadata, artists and tags etc.
            await self.__add_prov_ids(album_id, MediaType.Album, album.provider_ids, db)
            await self.__add_metadata(album_id, MediaType.Album, album.metadata, db)
            await self.__add_tags(album_id, MediaType.Album, album.tags, db)
            await self.__add_album_labels(album_id, album.labels, db)
            await self.__add_external_ids(album_id, MediaType.Album, album.external_ids, db)
            # save
            await db.commit()
        LOGGER.debug('added album %s (%s) to database: %s' %(album.name, album.provider_ids, album_id))
        return album_id

    async def tracks(self, custom_query=None, limit=100000, offset=0, orderby='name', fulldata=True) -> List[Track]:
        ''' fetch all track records from table'''
        tracks = []
        sql_query = 'SELECT * FROM tracks'
        if custom_query:
            sql_query = custom_query
        sql_query += ' ORDER BY %s' % orderby
        if limit:
            sql_query += ' LIMIT %d OFFSET %d' %(limit, offset)
        async with aiosqlite.connect(self.dbfile) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql_query) as cursor:
                for db_row in await cursor.fetchall():
                    track = Track()
                    track.item_id = db_row["track_id"]
                    track.name = db_row["name"]
                    track.album = await self.album(db_row["album_id"], fulldata=False, db=db)
                    track.artists = await self.__get_track_artists(track.item_id, db, fulldata=False)
                    track.duration = db_row["duration"]
                    track.version = db_row["version"]
                    track.disc_number = db_row["disc_number"]
                    track.track_number = db_row["track_number"]
                    try:
                        track.position = db_row["position"]
                    except IndexError:
                        pass
                    track.in_library = await self.__get_library_providers(track.item_id, MediaType.Track, db)
                    track.external_ids = await self.__get_external_ids(track.item_id, MediaType.Track, db)
                    track.provider_ids = await self.__get_prov_ids(track.item_id, MediaType.Track, db)
                    if fulldata:
                        track.metadata = await self.__get_metadata(track.item_id, MediaType.Track, db)
                        track.tags = await self.__get_tags(track.item_id, MediaType.Track, db)
                    tracks.append(track)
        return tracks

    async def track(self, track_id:int, fulldata=True) -> Track:
        ''' get track record by id '''
        track_id = try_parse_int(track_id)
        sql_query = "SELECT * FROM tracks WHERE track_id = %s" % track_id
        tracks = await self.tracks(sql_query, fulldata=fulldata)
        if not tracks:
            return None
        return tracks[0]

    async def add_track(self, track:Track):
        ''' add a new track record into table'''
        assert(track.name and track.album)
        track_id = None
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            # always try to grab existing track with external_id
            track_id = await self.__get_item_by_external_id(track, db)
            # fallback to matching on album_id, name and version or track number
            if not track_id and track.track_number:
                async with db.execute('SELECT (track_id) FROM tracks WHERE album_id=? AND track_number=?;', (track.album.item_id, track.track_number)) as cursor:
                    result = await cursor.fetchone()
                    if result:
                        track_id = result[0]
            if not track_id:
                async with db.execute('SELECT (track_id) FROM tracks WHERE album_id=? AND name=? AND version=?;', (track.album.item_id, track.name, track.version)) as cursor:
                    result = await cursor.fetchone()
                    if result:
                        track_id = result[0]
            if not track_id:
                # insert track
                assert(track.name and track.album.item_id)
                sql_query = 'INSERT OR IGNORE INTO tracks (name, album_id, duration, version, disc_number, track_number) VALUES(?,?,?,?,?,?);'
                await db.execute(sql_query, (track.name, track.album.item_id, track.duration, track.version, track.disc_number, track.track_number))
                await db.commit()
                # get id from newly created item (the safe way)
                async with db.execute('SELECT (track_id) FROM tracks WHERE name=? AND album_id=? AND version=?;', (track.name, track.album.item_id, track.version)) as cursor:
                    track_id = await cursor.fetchone()
                    assert(track_id)
                    track_id = track_id[0]
            # add track artists
            for artist in track.artists:
                sql_query = 'INSERT or IGNORE INTO track_artists (track_id, artist_id) VALUES(?,?);'
                await db.execute(sql_query, (track_id, artist.item_id))
            # add metadata, tags and artists etc.
            await self.__add_prov_ids(track_id, MediaType.Track, track.provider_ids, db)
            await self.__add_metadata(track_id, MediaType.Track, track.metadata, db)
            await self.__add_tags(track_id, MediaType.Track, track.tags, db)
            await self.__add_external_ids(track_id, MediaType.Track, track.external_ids, db)
            # save to db
            await db.commit()
        LOGGER.debug('added track %s (%s) to database: %s' %(track.name, track.provider_ids, track_id))
        return track_id

    async def update_track(self, track_id, column_key, column_value):
        ''' update column of existing track '''
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            sql_query = 'UPDATE tracks SET %s=%s WHERE track_id=%s;' %(column_key, column_value, track_id)
            await db.execute(sql_query)
            await db.commit()

    async def artist_tracks(self, artist_id, limit=1000000, offset=0, orderby='name') -> List[Track]:
        ''' get all library tracks for the given artist '''
        artist_id = try_parse_int(artist_id)
        sql_query = 'SELECT * FROM tracks WHERE track_id in (SELECT track_id FROM track_artists WHERE artist_id = %d)' % artist_id
        return await self.tracks(sql_query, limit=limit, offset=offset, orderby=orderby, fulldata=False)

    async def artist_albums(self, artist_id, limit=1000000, offset=0, orderby='name') -> List[Album]:
        ''' get all library albums for the given artist '''
        sql_query = ' WHERE artist_id = %d' % artist_id
        return await self.albums(sql_query, limit=limit, offset=offset, orderby=orderby, fulldata=False)

    async def playlist_tracks(self, playlist_id:int, limit=100000, offset=0, orderby='position') -> List[Track]:
        ''' get playlist tracks for the given playlist_id '''
        sql_query = """SELECT *, playlist_tracks.position FROM tracks
                    INNER JOIN playlist_tracks USING(track_id)
                    WHERE playlist_tracks.playlist_id=%s""" % playlist_id
        return await self.tracks(sql_query, orderby=orderby, limit=limit, offset=offset, fulldata=False)

    async def add_playlist_track(self, playlist_id:int, track_id, position):
        ''' add playlist track to playlist '''
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            sql_query = 'INSERT or REPLACE INTO playlist_tracks (playlist_id, track_id, position) VALUES(?,?,?);'
            await db.execute(sql_query, (playlist_id, track_id, position))
            await db.commit()

    async def remove_playlist_track(self, playlist_id:int, track_id):
        ''' remove playlist track from playlist '''
        async with aiosqlite.connect(self.dbfile, timeout=20) as db:
            sql_query = 'DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?;'
            await db.execute(sql_query, (playlist_id, track_id))
            await db.commit()
            
    async def set_track_loudness(self, provider_track_id, provider, loudness):
        ''' set integrated loudness for a track in db '''
        async with aiosqlite.connect(self.dbfile) as db:
            sql_query = 'INSERT or REPLACE INTO track_loudness (provider_track_id, provider, loudness) VALUES(?,?,?);'
            await db.execute(sql_query, (provider_track_id, provider, loudness))
            await db.commit()

    async def get_track_loudness(self, provider_track_id, provider):
        ''' get integrated loudness for a track in db '''
        async with aiosqlite.connect(self.dbfile) as db:
            sql_query = 'SELECT loudness FROM track_loudness WHERE provider_track_id = ? AND provider = ?'
            async with db.execute(sql_query, (provider_track_id, provider)) as cursor:
                result = await cursor.fetchone()
            if result:
                return result[0]
            else:
                return None

    async def __add_metadata(self, item_id, media_type, metadata, db):
        ''' add or update metadata'''
        for key, value in metadata.items():
            if value:
                sql_query = 'INSERT or REPLACE INTO metadata (item_id, media_type, key, value) VALUES(?,?,?,?);'
                await db.execute(sql_query, (item_id, media_type, key, value))

    async def __get_metadata(self, item_id, media_type, db, filter_key=None):
        ''' get metadata for media item '''
        metadata = {}
        sql_query = 'SELECT key, value FROM metadata WHERE item_id = ? AND media_type = ?'
        if filter_key:
            sql_query += ' AND key = "%s"' % filter_key
        async with db.execute(sql_query, (item_id, media_type)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            key = db_row[0]
            value = db_row[1]
            metadata[key] = value
        return metadata

    async def __add_tags(self, item_id, media_type, tags, db):
        ''' add tags to db '''
        for tag in tags:
            sql_query = 'INSERT or IGNORE INTO tags (name) VALUES(?);'
            async with db.execute(sql_query, (tag,)) as cursor:
                tag_id = cursor.lastrowid
            sql_query = 'INSERT or IGNORE INTO media_tags (item_id, media_type, tag_id) VALUES(?,?,?);'
            await db.execute(sql_query, (item_id, media_type, tag_id))

    async def __get_tags(self, item_id, media_type, db):
        ''' get tags for media item '''
        tags = []
        sql_query = 'SELECT name FROM tags INNER JOIN media_tags on tags.tag_id = media_tags.tag_id WHERE item_id = ? AND media_type = ?'
        async with db.execute(sql_query, (item_id, media_type)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            tags.append(db_row[0])
        return tags
    
    async def __add_album_labels(self, album_id, labels, db):
        ''' add labels to album in db '''
        for label in labels:
            sql_query = 'INSERT or IGNORE INTO labels (name) VALUES(?);'
            async with db.execute(sql_query, (label,)) as cursor:
                label_id = cursor.lastrowid
            sql_query = 'INSERT or IGNORE INTO album_labels (album_id, label_id) VALUES(?,?);'
            await db.execute(sql_query, (album_id, label_id))

    async def __get_album_labels(self, album_id, db):
        ''' get labels for album item '''
        labels = []
        sql_query = 'SELECT name FROM labels INNER JOIN album_labels on labels.label_id = album_labels.label_id WHERE album_id = ?'
        async with db.execute(sql_query, (album_id,)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            labels.append(db_row[0])
        return labels
    
    async def __get_track_artists(self, track_id, db, fulldata=False) -> List[Artist]:
        ''' get artists for track '''
        sql_query = 'WHERE artist_id in (SELECT artist_id FROM track_artists WHERE track_id = %s)' % track_id
        return await self.artists(sql_query, db=db, fulldata=fulldata)
    
    async def __add_external_ids(self, item_id, media_type, external_ids, db):
        ''' add or update external_ids'''
        for external_id in external_ids:
            for key, value in external_id.items():
                sql_query = 'INSERT or REPLACE INTO external_ids (item_id, media_type, key, value) VALUES(?,?,?,?);'
                await db.execute(sql_query, (item_id, media_type, key, value))

    async def __get_external_ids(self, item_id, media_type, db):
        ''' get external_ids for media item '''
        external_ids = []
        sql_query = 'SELECT key, value FROM external_ids WHERE item_id = ? AND media_type = ?'
        async with db.execute(sql_query, (item_id, media_type)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            external_id = {
                db_row[0]: db_row[1]
            }
            external_ids.append(external_id)
        return external_ids

    async def __add_prov_ids(self, item_id, media_type, provider_ids, db):
        ''' add provider ids for media item to db '''
        for prov_mapping in provider_ids:
            prov_id = prov_mapping['provider']
            prov_item_id = prov_mapping['item_id']
            quality = prov_mapping.get('quality',0)
            details = prov_mapping.get('details','')
            sql_query = 'INSERT OR REPLACE INTO provider_mappings (item_id, media_type, prov_item_id, provider, quality, details) VALUES(?,?,?,?,?,?);'
            await db.execute(sql_query, (item_id, media_type, prov_item_id, prov_id, quality, details))

    async def __get_prov_ids(self, item_id, media_type:MediaType, db):
        ''' get all provider_ids for media item '''
        provider_ids = []
        sql_query = 'SELECT prov_item_id, provider, quality, details \
            FROM provider_mappings \
            WHERE item_id = ? AND media_type = ?'
        async with db.execute(sql_query, (item_id, media_type)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            prov_mapping = {
                "provider": db_row[1],
                "item_id": db_row[0],
                "quality": db_row[2],
                "details": db_row[3]
            }
            provider_ids.append(prov_mapping)
        return provider_ids

    async def __get_library_providers(self, item_id, media_type:MediaType, db):
        ''' get the providers that have this media_item added to the library '''
        providers = []
        sql_query = 'SELECT provider FROM library_items WHERE item_id = ? AND media_type = ?'
        async with db.execute(sql_query, (item_id, media_type)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            providers.append( db_row[0] )
        return providers

    async def __get_item_by_external_id(self, media_item, db):
        ''' try to get existing item in db by matching the new item's external id's '''
        item_id = None
        for external_id in media_item.external_ids:
            if item_id:
                break
            for key, value in external_id.items():
                async with db.execute('SELECT (item_id) FROM external_ids WHERE media_type=? AND key=? AND value=?;', (media_item.media_type, key, value)) as cursor:
                    result = await cursor.fetchone()
                    if result:
                        item_id = result[0]
                        break
                if item_id:
                    break
        return item_id