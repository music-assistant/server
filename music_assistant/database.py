#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import asyncio
import logging
import os
from typing import List
import aiosqlite

from .utils import LOGGER, get_sort_name, try_parse_int
from .models.media_types import MediaType, Artist, Album, Track, Playlist, Radio


def commit_guard(func):
    """ decorator to guard against multiple db writes """
    async def wrapped(*args, **kwargs):
        method_class = args[0]
        while method_class.commit_guard_active:
            await asyncio.sleep(0.1)
        method_class.commit_guard_active = True
        res = await func(*args, **kwargs)
        method_class.commit_guard_active = False
        return res

    return wrapped


class Database():
    commit_guard_active = False

    def __init__(self, mass):
        self.mass = mass
        if not os.path.isdir(mass.datapath):
            raise FileNotFoundError(
                f"data directory {mass.datapath} does not exist!")
        self._dbfile = os.path.join(mass.datapath, "database.db")
        self._db = None
        logging.getLogger('aiosqlite').setLevel(logging.INFO)

    async def close(self):
        ''' handle shutdown event, close db connection '''
        await self._db.close()
        LOGGER.info("db connection closed")

    async def setup(self):
        ''' init database '''
        self._db = await aiosqlite.connect(self._dbfile)
        self._db.row_factory = aiosqlite.Row

        await self._db.execute('''CREATE TABLE IF NOT EXISTS library_items(
                item_id INTEGER NOT NULL, provider TEXT NOT NULL, 
                media_type INTEGER NOT NULL, UNIQUE(item_id, provider, media_type)
            );''')

        await self._db.execute('''CREATE TABLE IF NOT EXISTS artists(
                artist_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, 
                sort_name TEXT, musicbrainz_id TEXT NOT NULL UNIQUE);''')

        await self._db.execute('''CREATE TABLE IF NOT EXISTS albums(
                album_id INTEGER PRIMARY KEY AUTOINCREMENT, artist_id INTEGER NOT NULL, 
                name TEXT NOT NULL, albumtype TEXT, year INTEGER, version TEXT, 
                UNIQUE(artist_id, name, version, year)
            );''')

        await self._db.execute('''CREATE TABLE IF NOT EXISTS labels(
                label_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);'''
                               )
        await self._db.execute('''CREATE TABLE IF NOT EXISTS album_labels(
                album_id INTEGER, label_id INTEGER, UNIQUE(album_id, label_id));'''
                               )

        await self._db.execute('''CREATE TABLE IF NOT EXISTS tracks(
                track_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, 
                album_id INTEGER, version TEXT, duration INTEGER, 
                UNIQUE(name, version, album_id, duration)
            );''')
        await self._db.execute('''CREATE TABLE IF NOT EXISTS track_artists(
                track_id INTEGER, artist_id INTEGER, UNIQUE(track_id, artist_id));'''
                               )

        await self._db.execute('''CREATE TABLE IF NOT EXISTS tags(
                tag_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);'''
                               )
        await self._db.execute('''CREATE TABLE IF NOT EXISTS media_tags(
                item_id INTEGER, media_type INTEGER, tag_id, 
                UNIQUE(item_id, media_type, tag_id)
            );''')

        await self._db.execute('''CREATE TABLE IF NOT EXISTS provider_mappings(
                item_id INTEGER NOT NULL, media_type INTEGER NOT NULL, prov_item_id TEXT NOT NULL, 
                provider TEXT NOT NULL, quality INTEGER NOT NULL, details TEXT NULL, 
                UNIQUE(item_id, media_type, prov_item_id, provider, quality)
                );''')

        await self._db.execute('''CREATE TABLE IF NOT EXISTS metadata(
                item_id INTEGER NOT NULL, media_type INTEGER NOT NULL, key TEXT NOT NULL, 
                value TEXT, UNIQUE(item_id, media_type, key));''')

        await self._db.execute('''CREATE TABLE IF NOT EXISTS external_ids(
                item_id INTEGER NOT NULL, media_type INTEGER NOT NULL, key TEXT NOT NULL, 
                value TEXT, UNIQUE(item_id, media_type, key, value));''')

        await self._db.execute('''CREATE TABLE IF NOT EXISTS playlists(
                playlist_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, 
                owner TEXT NOT NULL, is_editable BOOLEAN NOT NULL, checksum TEXT NOT NULL, 
                UNIQUE(name, owner)
                );''')

        await self._db.execute('''CREATE TABLE IF NOT EXISTS radios(
                radio_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);'''
                               )

        await self._db.execute('''CREATE TABLE IF NOT EXISTS track_loudness(
                provider_track_id INTEGER NOT NULL, provider TEXT NOT NULL, loudness REAL, 
                UNIQUE(provider_track_id, provider));''')

        await self._db.commit()
        await self._db.execute('VACUUM;')

    async def get_database_id(self, provider: str, prov_item_id: str,
                              media_type: MediaType):
        ''' get the database id for the given prov_id '''
        if provider == 'database':
            return prov_item_id
        sql_query = '''SELECT item_id FROM provider_mappings
            WHERE prov_item_id = ? AND provider = ? AND media_type = ?;'''
        cursor = await self._db.execute(sql_query,
                                        (prov_item_id, provider, media_type))
        item_id = await cursor.fetchone()
        if item_id:
            item_id = item_id[0]
        await cursor.close()
        return item_id

    async def search(self, searchquery, media_types: List[MediaType]):
        ''' search library for the given searchphrase '''
        result = {"artists": [], "albums": [], "tracks": [], "playlists": []}
        searchquery = "%" + searchquery + "%"
        if MediaType.Artist in media_types:
            sql_query = ' WHERE name LIKE "%s"' % searchquery
            result["artists"] = [
                item async for item in self.artists(sql_query)
            ]
        if MediaType.Album in media_types:
            sql_query = ' WHERE name LIKE "%s"' % searchquery
            result["albums"] = [item async for item in self.albums(sql_query)]
        if MediaType.Track in media_types:
            sql_query = 'SELECT * FROM tracks WHERE name LIKE "%s"' % searchquery
            result["tracks"] = [item async for item in self.tracks(sql_query)]
        if MediaType.Playlist in media_types:
            sql_query = ' WHERE name LIKE "%s"' % searchquery
            result["playlists"] = [
                item async for item in self.library_playlists(sql_query)
            ]
        return result

    async def library_artists(self, provider=None,
                              orderby='name') -> List[Artist]:
        ''' get all library artists, optionally filtered by provider'''
        if provider is not None:
            sql_query = 'WHERE artist_id in (SELECT item_id FROM library_items WHERE provider = "%s" AND media_type = %d)' % (
                provider, MediaType.Artist)
        else:
            sql_query = 'WHERE artist_id in (SELECT item_id FROM library_items WHERE media_type = %d)' % MediaType.Artist
        async for item in self.artists(sql_query, orderby=orderby):
            yield item

    async def library_albums(self, provider=None,
                             orderby='name') -> List[Album]:
        ''' get all library albums, optionally filtered by provider'''
        if provider is not None:
            sql_query = ' WHERE album_id in (SELECT item_id FROM library_items WHERE provider = "%s" AND media_type = %d)' % (
                provider, MediaType.Album)
        else:
            sql_query = ' WHERE album_id in (SELECT item_id FROM library_items WHERE media_type = %d)' % MediaType.Album
        async for item in self.albums(sql_query, orderby=orderby):
            yield item

    async def library_tracks(self, provider=None,
                             orderby='name') -> List[Track]:
        ''' get all library tracks, optionally filtered by provider'''
        if provider is not None:
            sql_query = '''SELECT * FROM tracks
                WHERE track_id in (SELECT item_id FROM library_items WHERE provider = "%s" 
                AND media_type = %d)''' % (provider, MediaType.Track)
        else:
            sql_query = '''SELECT * FROM tracks
                WHERE track_id in
                (SELECT item_id FROM library_items WHERE media_type = %d)''' % MediaType.Track
        async for item in self.tracks(sql_query, orderby=orderby):
            yield item

    async def library_playlists(self, provider=None,
                                orderby='name') -> List[Playlist]:
        ''' fetch all playlist records from table'''
        if provider is not None:
            sql_query = '''WHERE playlist_id in
                (SELECT item_id FROM library_items WHERE provider = "%s"
                AND media_type = %d)''' % (provider, MediaType.Playlist)
        else:
            sql_query = '''WHERE playlist_id in
                (SELECT item_id FROM library_items WHERE media_type = %d)''' % MediaType.Playlist
        async for item in self.playlists(sql_query, orderby=orderby):
            yield item

    async def library_radios(self, provider=None,
                             orderby='name') -> List[Radio]:
        ''' fetch all radio records from table'''
        if provider is not None:
            sql_query = '''WHERE radio_id in 
                (SELECT item_id FROM library_items WHERE provider = "%s"
                AND media_type = %d)''' % (provider, MediaType.Radio)
        else:
            sql_query = '''WHERE radio_id in
                (SELECT item_id FROM library_items WHERE media_type = %d)''' % MediaType.Radio
        async for item in self.radios(sql_query, orderby=orderby):
            yield item

    async def playlists(self, filter_query=None,
                        orderby='name') -> List[Playlist]:
        ''' fetch playlist records from table'''
        sql_query = 'SELECT * FROM playlists'
        if filter_query:
            sql_query += ' ' + filter_query
        sql_query += ' ORDER BY %s' % orderby
        async with self._db.execute(sql_query) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            playlist = Playlist()
            playlist.item_id = db_row['playlist_id']
            playlist.name = db_row['name']
            playlist.owner = db_row['owner']
            playlist.is_editable = db_row['is_editable']
            playlist.checksum = db_row['checksum']
            playlist.metadata = await self.__get_metadata(
                playlist.item_id, MediaType.Playlist)
            playlist.provider_ids = await self.__get_prov_ids(
                playlist.item_id, MediaType.Playlist)
            playlist.in_library = await self.__get_library_providers(
                playlist.item_id, MediaType.Playlist)
            yield playlist

    async def playlist(self, playlist_id: int) -> Playlist:
        ''' get playlist record by id '''
        playlist_id = try_parse_int(playlist_id)
        async for item in self.playlists('WHERE playlist_id = %s' %
                                         playlist_id):
            return item
        return None

    async def radios(self, filter_query=None,
                     orderby='name') -> List[Playlist]:
        ''' fetch radio records from table'''
        sql_query = 'SELECT * FROM radios'
        if filter_query:
            sql_query += ' ' + filter_query
        sql_query += ' ORDER BY %s' % orderby
        async with self._db.execute(sql_query) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            radio = Radio()
            radio.item_id = db_row[0]
            radio.name = db_row[1]
            radio.metadata = await self.__get_metadata(radio.item_id,
                                                       MediaType.Radio)
            radio.provider_ids = await self.__get_prov_ids(
                radio.item_id, MediaType.Radio)
            radio.in_library = await self.__get_library_providers(
                radio.item_id, MediaType.Radio)
            yield radio

    async def radio(self, radio_id: int) -> Playlist:
        ''' get radio record by id '''
        radio_id = try_parse_int(radio_id)
        async for item in self.radios('WHERE radio_id = %s' % radio_id):
            return item
        return None

    @commit_guard
    async def add_playlist(self, playlist: Playlist):
        ''' add a new playlist record into table'''
        assert (playlist.name)
        async with self._db.execute(
                'SELECT (playlist_id) FROM playlists WHERE name=? AND owner=?;',
            (playlist.name, playlist.owner)) as cursor:
            result = await cursor.fetchone()
            if result:
                playlist_id = result[0]
                # update existing
                sql_query = 'UPDATE playlists SET is_editable=?, checksum=? WHERE playlist_id=?;'
                await self._db.execute(
                    sql_query,
                    (playlist.is_editable, playlist.checksum, playlist_id))
            else:
                # insert playlist
                sql_query = 'INSERT INTO playlists (name, owner, is_editable, checksum) VALUES(?,?,?,?);'
                async with self._db.execute(
                        sql_query,
                    (playlist.name, playlist.owner, playlist.is_editable,
                     playlist.checksum)) as cursor:
                    last_row_id = cursor.lastrowid
                    await self._db.commit()
                # get id from newly created item
                sql_query = 'SELECT (playlist_id) FROM playlists WHERE ROWID=?'
                async with self._db.execute(sql_query,
                                            (last_row_id, )) as cursor:
                    playlist_id = await cursor.fetchone()
                    playlist_id = playlist_id[0]
                LOGGER.debug('added playlist %s to database: %s',
                             playlist.name, playlist_id)
            # add/update metadata
            await self.__add_prov_ids(playlist_id, MediaType.Playlist,
                                      playlist.provider_ids)
            await self.__add_metadata(playlist_id, MediaType.Playlist,
                                      playlist.metadata)
            # save
            await self._db.commit()
        return playlist_id

    @commit_guard
    async def add_radio(self, radio: Radio):
        ''' add a new radio record into table'''
        assert (radio.name)
        async with self._db.execute(
                'SELECT (radio_id) FROM radios WHERE name=?;',
            (radio.name, )) as cursor:
            result = await cursor.fetchone()
            if result:
                radio_id = result[0]
            else:
                # insert radio
                sql_query = 'INSERT INTO radios (name) VALUES(?);'
                async with self._db.execute(sql_query,
                                            (radio.name, )) as cursor:
                    last_row_id = cursor.lastrowid
                    await self._db.commit()
                # get id from newly created item
                sql_query = 'SELECT (radio_id) FROM radios WHERE ROWID=?'
                async with self._db.execute(sql_query,
                                            (last_row_id, )) as cursor:
                    radio_id = await cursor.fetchone()
                    radio_id = radio_id[0]
                LOGGER.debug('added radio station %s to database: %s',
                             radio.name, radio_id)
            # add/update metadata
            await self.__add_prov_ids(radio_id, MediaType.Radio,
                                      radio.provider_ids)
            await self.__add_metadata(radio_id, MediaType.Radio,
                                      radio.metadata)
            # save
            await self._db.commit()
        return radio_id

    async def add_to_library(self, item_id: int, media_type: MediaType,
                             provider: str):
        ''' add an item to the library (item must already be present in the db!) '''
        item_id = try_parse_int(item_id)
        sql_query = 'INSERT or REPLACE INTO library_items (item_id, provider, media_type) VALUES(?,?,?);'
        await self._db.execute(sql_query, (item_id, provider, media_type))
        await self._db.commit()

    async def remove_from_library(self, item_id: int, media_type: MediaType,
                                  provider: str):
        ''' remove item from the library '''
        item_id = try_parse_int(item_id)
        sql_query = 'DELETE FROM library_items WHERE item_id=? AND provider=? AND media_type=?;'
        await self._db.execute(sql_query, (item_id, provider, media_type))
        if media_type == MediaType.Playlist:
            sql_query = 'DELETE FROM playlists WHERE playlist_id=?;'
            await self._db.execute(sql_query, (item_id, ))
            sql_query = 'DELETE FROM provider_mappings WHERE item_id=? AND media_type=? AND provider=?;'
            await self._db.execute(sql_query, (item_id, media_type, provider))
            await self._db.commit()

    async def artists(self, filter_query=None, orderby='name',
                      fulldata=False) -> List[Artist]:
        ''' fetch artist records from table'''
        sql_query = 'SELECT * FROM artists'
        if filter_query:
            sql_query += ' ' + filter_query
        sql_query += ' ORDER BY %s' % orderby
        async with self._db.execute(sql_query) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            artist = Artist()
            artist.item_id = db_row[0]
            artist.name = db_row[1]
            artist.sort_name = db_row[2]
            artist.provider_ids = await self.__get_prov_ids(
                artist.item_id, MediaType.Artist)
            artist.in_library = await self.__get_library_providers(
                artist.item_id, MediaType.Artist)
            if fulldata:
                artist.external_ids = await self.__get_external_ids(
                    artist.item_id, MediaType.Artist)
                artist.metadata = await self.__get_metadata(
                    artist.item_id, MediaType.Artist)
                artist.tags = await self.__get_tags(artist.item_id,
                                                    MediaType.Artist)
                artist.metadata = await self.__get_metadata(
                    artist.item_id, MediaType.Artist)
            yield artist

    async def artist(self, artist_id: int, fulldata=True) -> Artist:
        ''' get artist record by id '''
        artist_id = try_parse_int(artist_id)
        async for item in self.artists('WHERE artist_id = %s' % artist_id,
                                       fulldata=fulldata):
            return item
        return None

    @commit_guard
    async def add_artist(self, artist: Artist):
        ''' add a new artist record into table'''
        artist_id = None
        # always prefer to grab existing artist with external_id (=musicbrainz_id)
        artist_id = await self.__get_item_by_external_id(artist)
        if not artist_id:
            # insert artist
            musicbrainz_id = None
            for item in artist.external_ids:
                if item.get('musicbrainz'):
                    musicbrainz_id = item['musicbrainz']
                    break
            assert (musicbrainz_id)  # musicbrainz id is required
            if not artist.sort_name:
                artist.sort_name = get_sort_name(artist.name)
            sql_query = 'INSERT INTO artists (name, sort_name, musicbrainz_id) VALUES(?,?,?);'
            async with self._db.execute(
                    sql_query,
                (artist.name, artist.sort_name, musicbrainz_id)) as cursor:
                last_row_id = cursor.lastrowid
            # get id from (newly created) item
            async with self._db.execute(
                    'SELECT artist_id FROM artists WHERE ROWID=?;',
                (last_row_id, )) as cursor:
                artist_id = await cursor.fetchone()
                artist_id = artist_id[0]
        # always add metadata and tags etc. because we might have received
        # additional info or a match from other provider
        await self.__add_prov_ids(artist_id, MediaType.Artist,
                                  artist.provider_ids)
        await self.__add_metadata(artist_id, MediaType.Artist, artist.metadata)
        await self.__add_tags(artist_id, MediaType.Artist, artist.tags)
        await self.__add_external_ids(artist_id, MediaType.Artist,
                                      artist.external_ids)
        # save
        await self._db.commit()
        LOGGER.debug('added artist %s (%s) to database: %s', artist.name,
                     artist.provider_ids, artist_id)
        return artist_id

    async def albums(self, filter_query=None, orderby='name',
                     fulldata=False) -> List[Album]:
        ''' fetch all album records from table'''
        sql_query = 'SELECT * FROM albums'
        if filter_query:
            sql_query += ' ' + filter_query
        sql_query += ' ORDER BY %s' % orderby
        async with self._db.execute(sql_query) as cursor:
            db_rows = await cursor.fetchall()
            for db_row in db_rows:
                album = Album()
                album.item_id = db_row[0]
                album.name = db_row[2]
                album.albumtype = db_row[3]
                album.year = db_row[4]
                album.version = db_row[5]
                album.provider_ids = await self.__get_prov_ids(
                    album.item_id, MediaType.Album)
                album.in_library = await self.__get_library_providers(
                    album.item_id, MediaType.Album)
                album.artist = await self.artist(db_row[1], fulldata=fulldata)
                if fulldata:
                    album.external_ids = await self.__get_external_ids(
                        album.item_id, MediaType.Album)
                    album.metadata = await self.__get_metadata(
                        album.item_id, MediaType.Album)
                    album.tags = await self.__get_tags(album.item_id,
                                                       MediaType.Album)
                    album.labels = await self.__get_album_labels(album.item_id)
                yield album

    async def album(self, album_id: int, fulldata=True) -> Album:
        ''' get album record by id '''
        album_id = try_parse_int(album_id)
        async for item in self.albums('WHERE album_id = %s' % album_id,
                                      fulldata=fulldata):
            return item
        return None

    @commit_guard
    async def add_album(self, album: Album):
        ''' add a new album record into table'''
        assert (album.name and album.artist)
        album_id = None
        assert (album.artist.provider == 'database')
        # always try to grab existing album with external_id
        album_id = await self.__get_item_by_external_id(album)
        # fallback to matching on artist_id, name and version
        if not album_id:
            # search exact match first
            sql_query = 'SELECT album_id FROM albums WHERE artist_id=? AND name=? AND version=? AND year=? AND albumtype=?'
            async with self._db.execute(
                    sql_query,
                (album.artist.item_id, album.name, album.version, album.year,
                 album.albumtype)) as cursor:
                album_id = await cursor.fetchone()
                if album_id:
                    album_id = album_id['album_id']
            # fallback to almost exact match
            sql_query = 'SELECT album_id, year, version, albumtype FROM albums WHERE artist_id=? AND name=?'
            async with self._db.execute(
                    sql_query, (album.artist.item_id, album.name)) as cursor:
                albums = await cursor.fetchall()
                for result in albums:
                    if ((not album.version and result['year'] == album.year)
                            or (album.version
                                and result['version'] == album.version)):
                        album_id = result['album_id']
                        break
        if not album_id:
            # insert album
            sql_query = 'INSERT INTO albums (artist_id, name, albumtype, year, version) VALUES(?,?,?,?,?);'
            query_params = (album.artist.item_id, album.name, album.albumtype,
                            album.year, album.version)
            async with self._db.execute(sql_query, query_params) as cursor:
                last_row_id = cursor.lastrowid
            # get id from newly created item
            sql_query = 'SELECT (album_id) FROM albums WHERE ROWID=?'
            async with self._db.execute(sql_query, (last_row_id, )) as cursor:
                album_id = await cursor.fetchone()
                album_id = album_id[0]
        # always add metadata and tags etc. because we might have received
        # additional info or a match from other provider
        await self.__add_prov_ids(album_id, MediaType.Album,
                                  album.provider_ids)
        await self.__add_metadata(album_id, MediaType.Album, album.metadata)
        await self.__add_tags(album_id, MediaType.Album, album.tags)
        await self.__add_album_labels(album_id, album.labels)
        await self.__add_external_ids(album_id, MediaType.Album,
                                      album.external_ids)
        # save
        await self._db.commit()
        LOGGER.debug('added album %s (%s) to database: %s', album.name,
                     album.provider_ids, album_id)
        return album_id

    async def tracks(self, custom_query=None, orderby='name',
                     fulldata=False) -> List[Track]:
        ''' fetch all track records from table'''
        sql_query = 'SELECT * FROM tracks'
        if custom_query:
            sql_query = custom_query
        sql_query += ' ORDER BY %s' % orderby
        async with self._db.execute(sql_query) as cursor:
            for db_row in await cursor.fetchall():
                track = Track()
                track.item_id = db_row["track_id"]
                track.name = db_row["name"]
                track.album = await self.album(db_row["album_id"],
                                               fulldata=fulldata)
                track.artists = await self.__get_track_artists(
                    track.item_id, fulldata=fulldata)
                track.duration = db_row["duration"]
                track.version = db_row["version"]
                try:  # album tracks only
                    track.disc_number = db_row["disc_number"]
                    track.track_number = db_row["track_number"]
                except IndexError:
                    pass
                try:  # playlist tracks only
                    track.position = db_row["position"]
                except IndexError:
                    pass
                track.in_library = await self.__get_library_providers(
                    track.item_id, MediaType.Track)
                track.external_ids = await self.__get_external_ids(
                    track.item_id, MediaType.Track)
                track.provider_ids = await self.__get_prov_ids(
                    track.item_id, MediaType.Track)
                if fulldata:
                    track.metadata = await self.__get_metadata(
                        track.item_id, MediaType.Track)
                    track.tags = await self.__get_tags(track.item_id,
                                                       MediaType.Track)
                yield track

    async def track(self, track_id: int, fulldata=True) -> Track:
        ''' get track record by id '''
        track_id = try_parse_int(track_id)
        sql_query = "SELECT * FROM tracks WHERE track_id = %s" % track_id
        async for item in self.tracks(sql_query, fulldata=fulldata):
            return item
        return None

    @commit_guard
    async def add_track(self, track: Track):
        ''' add a new track record into table'''
        assert (track.name and track.album)
        assert (track.album.provider == 'database')
        assert (track.artists)
        for artist in track.artists:
            assert (artist.provider == 'database')
        # always try to grab existing track with external_id
        track_id = await self.__get_item_by_external_id(track)
        # fallback to matching on album_id, name and version
        if not track_id:
            sql_query = 'SELECT track_id, duration, version FROM tracks WHERE album_id=? AND name=?'
            async with self._db.execute(
                    sql_query, (track.album.item_id, track.name)) as cursor:
                results = await cursor.fetchall()
                for result in results:
                    # we perform an additional safety check on the duration or version
                    if ((track.version and result['version'] == track.version)
                            or
                        (not track.version
                         and abs(result['duration'] - track.duration) < 3)):
                        track_id = result['track_id']
                        break
        if not track_id:
            # insert track
            assert (track.name and track.album.item_id)
            sql_query = 'INSERT INTO tracks (name, album_id, duration, version) VALUES(?,?,?,?);'
            query_params = (track.name, track.album.item_id, track.duration,
                            track.version)
            async with self._db.execute(sql_query, query_params) as cursor:
                last_row_id = cursor.lastrowid
            # get id from newly created item (the safe way)
            async with self._db.execute(
                    'SELECT track_id FROM tracks WHERE ROWID=?',
                (last_row_id, )) as cursor:
                track_id = await cursor.fetchone()
                track_id = track_id[0]
        # add track artists
        for artist in track.artists:
            sql_query = 'INSERT or IGNORE INTO track_artists (track_id, artist_id) VALUES(?,?);'
            await self._db.execute(sql_query, (track_id, artist.item_id))
        # always add metadata and tags etc. because we might have received
        # additional info or a match from other provider
        await self.__add_prov_ids(track_id, MediaType.Track,
                                  track.provider_ids)
        await self.__add_metadata(track_id, MediaType.Track, track.metadata)
        await self.__add_tags(track_id, MediaType.Track, track.tags)
        await self.__add_external_ids(track_id, MediaType.Track,
                                      track.external_ids)
        # save to db
        await self._db.commit()
        LOGGER.debug('added track %s (%s) to database: %s', track.name,
                     track.provider_ids, track_id)
        return track_id

    async def update_track(self, track_id, column_key, column_value):
        ''' update column of existing track '''
        sql_query = 'UPDATE tracks SET %s=? WHERE track_id=?;' % column_key
        await self._db.execute(sql_query, (column_value, track_id))
        await self._db.commit()

    async def update_playlist(self, playlist_id, column_key, column_value):
        ''' update column of existing playlist '''
        sql_query = 'UPDATE playlists SET %s=? WHERE playlist_id=?;' % column_key
        await self._db.execute(sql_query, (column_value, playlist_id))
        await self._db.commit()

    async def artist_tracks(self, artist_id, orderby='name') -> List[Track]:
        ''' get all library tracks for the given artist '''
        artist_id = try_parse_int(artist_id)
        sql_query = 'SELECT * FROM tracks WHERE track_id in (SELECT track_id FROM track_artists WHERE artist_id = %s)' % artist_id
        async for item in self.tracks(sql_query,
                                      orderby=orderby,
                                      fulldata=False):
            yield item

    async def artist_albums(self, artist_id, orderby='name') -> List[Album]:
        ''' get all library albums for the given artist '''
        sql_query = ' WHERE artist_id = %s' % artist_id
        async for item in self.albums(sql_query,
                                      orderby=orderby,
                                      fulldata=False):
            yield item

    async def set_track_loudness(self, provider_track_id, provider, loudness):
        ''' set integrated loudness for a track in db '''
        sql_query = 'INSERT or REPLACE INTO track_loudness (provider_track_id, provider, loudness) VALUES(?,?,?);'
        await self._db.execute(sql_query,
                               (provider_track_id, provider, loudness))
        await self._db.commit()

    async def get_track_loudness(self, provider_track_id, provider):
        ''' get integrated loudness for a track in db '''
        sql_query = 'SELECT loudness FROM track_loudness WHERE provider_track_id = ? AND provider = ?'
        async with self._db.execute(sql_query,
                                    (provider_track_id, provider)) as cursor:
            result = await cursor.fetchone()
        if result:
            return result[0]
        else:
            return None

    async def __add_metadata(self, item_id, media_type, metadata):
        ''' add or update metadata'''
        for key, value in metadata.items():
            if value:
                sql_query = 'INSERT or REPLACE INTO metadata (item_id, media_type, key, value) VALUES(?,?,?,?);'
                await self._db.execute(sql_query,
                                       (item_id, media_type, key, value))

    async def __get_metadata(self, item_id, media_type, filter_key=None):
        ''' get metadata for media item '''
        metadata = {}
        sql_query = 'SELECT key, value FROM metadata WHERE item_id = ? AND media_type = ?'
        if filter_key:
            sql_query += ' AND key = "%s"' % filter_key
        async with self._db.execute(sql_query,
                                    (item_id, media_type)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            key = db_row[0]
            value = db_row[1]
            metadata[key] = value
        return metadata

    async def __add_tags(self, item_id, media_type, tags):
        ''' add tags to db '''
        for tag in tags:
            sql_query = 'INSERT or IGNORE INTO tags (name) VALUES(?);'
            async with self._db.execute(sql_query, (tag, )) as cursor:
                tag_id = cursor.lastrowid
            sql_query = 'INSERT or IGNORE INTO media_tags (item_id, media_type, tag_id) VALUES(?,?,?);'
            await self._db.execute(sql_query, (item_id, media_type, tag_id))

    async def __get_tags(self, item_id, media_type):
        ''' get tags for media item '''
        tags = []
        sql_query = 'SELECT name FROM tags INNER JOIN media_tags on tags.tag_id = media_tags.tag_id WHERE item_id = ? AND media_type = ?'
        async with self._db.execute(sql_query,
                                    (item_id, media_type)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            tags.append(db_row[0])
        return tags

    async def __add_album_labels(self, album_id, labels):
        ''' add labels to album in db '''
        for label in labels:
            sql_query = 'INSERT or IGNORE INTO labels (name) VALUES(?);'
            async with self._db.execute(sql_query, (label, )) as cursor:
                label_id = cursor.lastrowid
            sql_query = 'INSERT or IGNORE INTO album_labels (album_id, label_id) VALUES(?,?);'
            await self._db.execute(sql_query, (album_id, label_id))

    async def __get_album_labels(self, album_id):
        ''' get labels for album item '''
        labels = []
        sql_query = 'SELECT name FROM labels INNER JOIN album_labels on labels.label_id = album_labels.label_id WHERE album_id = ?'
        async with self._db.execute(sql_query, (album_id, )) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            labels.append(db_row[0])
        return labels

    async def __get_track_artists(self, track_id,
                                  fulldata=False) -> List[Artist]:
        ''' get artists for track '''
        sql_query = 'WHERE artist_id in (SELECT artist_id FROM track_artists WHERE track_id = %s)' % track_id
        return [
            item async for item in self.artists(sql_query, fulldata=fulldata)
        ]

    async def __add_external_ids(self, item_id, media_type, external_ids):
        ''' add or update external_ids'''
        for external_id in external_ids:
            for key, value in external_id.items():
                sql_query = 'INSERT or REPLACE INTO external_ids (item_id, media_type, key, value) VALUES(?,?,?,?);'
                await self._db.execute(sql_query,
                                       (item_id, media_type, key, value))

    async def __get_external_ids(self, item_id, media_type):
        ''' get external_ids for media item '''
        external_ids = []
        sql_query = 'SELECT key, value FROM external_ids WHERE item_id = ? AND media_type = ?'
        async with self._db.execute(sql_query,
                                    (item_id, media_type)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            external_id = {db_row[0]: db_row[1]}
            external_ids.append(external_id)
        return external_ids

    async def __add_prov_ids(self, item_id, media_type, provider_ids):
        ''' add provider ids for media item to db '''
        for prov_mapping in provider_ids:
            prov_id = prov_mapping['provider']
            prov_item_id = prov_mapping['item_id']
            quality = prov_mapping.get('quality', 0)
            details = prov_mapping.get('details', '')
            sql_query = 'INSERT OR REPLACE INTO provider_mappings (item_id, media_type, prov_item_id, provider, quality, details) VALUES(?,?,?,?,?,?);'
            await self._db.execute(
                sql_query,
                (item_id, media_type, prov_item_id, prov_id, quality, details))

    async def __get_prov_ids(self, item_id, media_type: MediaType):
        ''' get all provider_ids for media item '''
        provider_ids = []
        sql_query = 'SELECT prov_item_id, provider, quality, details \
            FROM provider_mappings \
            WHERE item_id = ? AND media_type = ?'

        async with self._db.execute(sql_query,
                                    (item_id, media_type)) as cursor:
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

    async def __get_library_providers(self, item_id, media_type: MediaType):
        ''' get the providers that have this media_item added to the library '''
        providers = []
        sql_query = 'SELECT provider FROM library_items WHERE item_id = ? AND media_type = ?'
        async with self._db.execute(sql_query,
                                    (item_id, media_type)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            providers.append(db_row[0])
        return providers

    async def __get_item_by_external_id(self, media_item):
        ''' try to get existing item in db by matching the new item's external id's '''
        item_id = None
        for external_id in media_item.external_ids:
            if item_id:
                break
            for key, value in external_id.items():
                async with self._db.execute(
                        'SELECT (item_id) FROM external_ids WHERE media_type=? AND key=? AND value=?;',
                    (media_item.media_type, key, value)) as cursor:
                    result = await cursor.fetchone()
                    if result:
                        item_id = result[0]
                        break
                if item_id:
                    break
        return item_id