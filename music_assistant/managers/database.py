"""Database logic."""
# pylint: disable=too-many-lines
import logging
import os
from functools import partial
from typing import List

import aiosqlite
from music_assistant.helpers.util import (
    compare_strings,
    merge_dict,
    merge_list,
    try_parse_int,
)
from music_assistant.helpers.web import json_serializer
from music_assistant.models.media_types import (
    Album,
    AlbumArtist,
    Artist,
    MediaItemProviderId,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    Track,
    TrackAlbum,
    TrackArtist,
)

LOGGER = logging.getLogger("database")


class DbConnect:
    """Helper to initialize the db connection or utilize an existing one."""

    def __init__(self, dbfile: str, db_conn: aiosqlite.Connection = None):
        """Initialize class."""
        self._db_conn_provided = db_conn is not None
        self._db_conn = db_conn
        self._dbfile = dbfile

    async def __aenter__(self):
        """Enter."""
        if not self._db_conn_provided:
            self._db_conn = await aiosqlite.connect(self._dbfile, timeout=120)
        return self._db_conn

    async def __aexit__(self, exc_type, exc_value, traceback):
        """Exit."""
        if not self._db_conn_provided:
            await self._db_conn.close()
        return False


class DatabaseManager:
    """Class that holds the (logic to the) database."""

    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        self._dbfile = os.path.join(mass.config.data_path, "mass.db")
        self.db_conn = partial(DbConnect, self._dbfile)
        self.cache = {}

    async def async_setup(self):
        """Async initialization."""
        async with DbConnect(self._dbfile) as db_conn:

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS provider_mappings(
                    item_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    prov_item_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    quality INTEGER NOT NULL,
                    details TEXT NULL,
                    UNIQUE(item_id, media_type, prov_item_id, provider, quality)
                    );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS artists(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT,
                    musicbrainz_id TEXT NOT NULL UNIQUE,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json
                    );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS albums(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT,
                    album_type TEXT,
                    year INTEGER,
                    version TEXT,
                    in_library BOOLEAN DEFAULT 0,
                    upc TEXT,
                    artist json,
                    metadata json,
                    provider_ids json,
                    UNIQUE(item_id, name, version, year)
                );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS tracks(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT,
                    version TEXT,
                    duration INTEGER,
                    in_library BOOLEAN DEFAULT 0,
                    isrc TEXT,
                    album json,
                    artists json,
                    metadata json,
                    provider_ids json,
                    UNIQUE(name, version, item_id, duration)
                );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS playlists(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT,
                    owner TEXT NOT NULL,
                    is_editable BOOLEAN NOT NULL,
                    checksum TEXT NOT NULL,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json,
                    UNIQUE(name, owner)
                    );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS radios(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    sort_name TEXT,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json
                    );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS track_loudness(
                    provider_item_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    loudness REAL,
                    UNIQUE(provider_item_id, provider));"""
            )

            await db_conn.commit()
            await db_conn.execute("VACUUM;")
            await db_conn.commit()

    async def async_get_item_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
        media_type: MediaType,
        db_conn: aiosqlite.Connection = None,
    ) -> int:
        """Get the database item for the given prov_id."""
        if media_type == MediaType.Artist:
            return await self.async_get_artist_by_prov_id(
                provider_id, prov_item_id, db_conn
            )
        if media_type == MediaType.Album:
            return await self.async_get_album_by_prov_id(
                provider_id, prov_item_id, db_conn
            )
        if media_type == MediaType.Track:
            return await self.async_get_track_by_prov_id(
                provider_id, prov_item_id, db_conn
            )
        if media_type == MediaType.Playlist:
            return await self.async_get_playlist_by_prov_id(
                provider_id, prov_item_id, db_conn
            )
        if media_type == MediaType.Radio:
            return await self.async_get_radio_by_prov_id(
                provider_id, prov_item_id, db_conn
            )
        return None

    async def async_get_track_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
        db_conn: aiosqlite.Connection = None,
    ) -> int:
        """Get the database track for the given prov_id."""
        if provider_id == "database":
            return await self.async_get_track(prov_item_id, db_conn=db_conn)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'track')"""
        for item in await self.async_get_tracks(sql_query, db_conn=db_conn):
            return item
        return None

    async def async_get_album_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
        db_conn: aiosqlite.Connection = None,
    ) -> int:
        """Get the database album for the given prov_id."""
        if provider_id == "database":
            return await self.async_get_album(prov_item_id, db_conn=db_conn)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'album')"""
        for item in await self.async_get_albums(sql_query, db_conn=db_conn):
            return item
        return None

    async def async_get_artist_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
        db_conn: aiosqlite.Connection = None,
    ) -> int:
        """Get the database artist for the given prov_id."""
        if provider_id == "database":
            return await self.async_get_artist(prov_item_id, db_conn=db_conn)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'artist')"""
        for item in await self.async_get_artists(sql_query, db_conn=db_conn):
            return item
        return None

    async def async_get_playlist_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
        db_conn: aiosqlite.Connection = None,
    ) -> int:
        """Get the database playlist for the given prov_id."""
        if provider_id == "database":
            return await self.async_get_playlist(prov_item_id, db_conn=db_conn)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'playlist')"""
        for item in await self.async_get_playlists(sql_query, db_conn=db_conn):
            return item
        return None

    async def async_get_radio_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
        db_conn: aiosqlite.Connection = None,
    ) -> int:
        """Get the database radio for the given prov_id."""
        if provider_id == "database":
            return await self.async_get_radio(prov_item_id, db_conn=db_conn)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'radio')"""
        for item in await self.async_get_radios(sql_query, db_conn=db_conn):
            return item
        return None

    async def async_search(
        self, searchquery: str, media_types: List[MediaType]
    ) -> SearchResult:
        """Search library for the given searchphrase."""
        async with DbConnect(self._dbfile) as db_conn:
            result = SearchResult([], [], [], [], [])
            searchquery = "%" + searchquery + "%"
            if media_types is None or MediaType.Artist in media_types:
                sql_query = ' WHERE name LIKE "%s"' % searchquery
                result.artists = await self.async_get_artists(
                    sql_query, db_conn=db_conn
                )
            if media_types is None or MediaType.Album in media_types:
                sql_query = ' WHERE name LIKE "%s"' % searchquery
                result.albums = await self.async_get_albums(sql_query, db_conn=db_conn)
            if media_types is None or MediaType.Track in media_types:
                sql_query = ' WHERE name LIKE "%s"' % searchquery
                result.tracks = await self.async_get_tracks(sql_query, db_conn=db_conn)
            if media_types is None or MediaType.Playlist in media_types:
                sql_query = ' WHERE name LIKE "%s"' % searchquery
                result.playlists = await self.async_get_playlists(
                    sql_query, db_conn=db_conn
                )
            if media_types is None or MediaType.Radio in media_types:
                sql_query = ' WHERE name LIKE "%s"' % searchquery
                result.radios = await self.async_get_radios(sql_query, db_conn=db_conn)
            return result

    async def async_get_library_artists(self, orderby: str = "name") -> List[Artist]:
        """Get all library artists."""
        sql_query = "WHERE in_library = 1"
        return await self.async_get_artists(sql_query, orderby=orderby)

    async def async_get_library_albums(self, orderby: str = "name") -> List[Album]:
        """Get all library albums."""
        sql_query = "WHERE in_library = 1"
        return await self.async_get_albums(sql_query, orderby=orderby)

    async def async_get_library_tracks(self, orderby: str = "name") -> List[Track]:
        """Get all library tracks."""
        sql_query = "WHERE in_library = 1"
        return await self.async_get_tracks(sql_query, orderby=orderby)

    async def async_get_library_playlists(
        self, orderby: str = "name"
    ) -> List[Playlist]:
        """Fetch all playlist records from table."""
        sql_query = "WHERE in_library = 1"
        return await self.async_get_playlists(sql_query, orderby=orderby)

    async def async_get_library_radios(
        self, provider_id: str = None, orderby: str = "name"
    ) -> List[Radio]:
        """Fetch all radio records from table."""
        sql_query = "WHERE in_library = 1"
        return await self.async_get_radios(sql_query, orderby=orderby)

    async def async_get_playlists(
        self,
        filter_query: str = None,
        orderby: str = "name",
        db_conn: aiosqlite.Connection = None,
    ) -> List[Playlist]:
        """Get all playlists from database."""
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            sql_query = "SELECT * FROM playlists"
            if filter_query:
                sql_query += " " + filter_query
            sql_query += " ORDER BY %s" % orderby
            return [
                Playlist.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def async_get_playlist(
        self, item_id: int, db_conn: aiosqlite.Connection = None
    ) -> Playlist:
        """Get playlist record by id."""
        item_id = try_parse_int(item_id)
        for item in await self.async_get_playlists(
            f"WHERE item_id = {item_id}", db_conn=db_conn
        ):
            return item
        return None

    async def async_get_radios(
        self,
        filter_query: str = None,
        orderby: str = "name",
        db_conn: aiosqlite.Connection = None,
    ) -> List[Radio]:
        """Fetch radio records from database."""
        sql_query = "SELECT * FROM radios"
        if filter_query:
            sql_query += " " + filter_query
        sql_query += " ORDER BY %s" % orderby
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            return [
                Radio.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def async_get_radio(
        self, item_id: int, db_conn: aiosqlite.Connection = None
    ) -> Playlist:
        """Get radio record by id."""
        item_id = try_parse_int(item_id)
        for item in await self.async_get_radios(
            f"WHERE item_id = {item_id}", db_conn=db_conn
        ):
            return item
        return None

    async def async_add_playlist(self, playlist: Playlist):
        """Add a new playlist record to the database."""
        assert playlist.name
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = await self.__execute_fetchone(
                db_conn,
                "SELECT (item_id) FROM playlists WHERE name=? AND owner=?;",
                (playlist.name, playlist.owner),
            )

            if cur_item:
                # update existing
                return await self.async_update_playlist(cur_item[0], playlist)
            # insert playlist
            sql_query = """INSERT INTO playlists
                (name, sort_name, owner, is_editable, checksum, metadata, provider_ids)
                VALUES(?,?,?,?,?,?,?);"""
            async with db_conn.execute(
                sql_query,
                (
                    playlist.name,
                    playlist.sort_name,
                    playlist.owner,
                    playlist.is_editable,
                    playlist.checksum,
                    json_serializer(playlist.metadata),
                    json_serializer(playlist.provider_ids),
                ),
            ) as cursor:
                last_row_id = cursor.lastrowid
                new_item = await self.__execute_fetchone(
                    db_conn,
                    "SELECT (item_id) FROM playlists WHERE ROWID=?;",
                    (last_row_id,),
                )
            await self.__async_add_prov_ids(
                new_item[0], MediaType.Playlist, playlist.provider_ids, db_conn
            )
            await db_conn.commit()
        LOGGER.debug("added playlist %s to database", playlist.name)
        # return created object
        return await self.async_get_playlist(new_item[0])

    async def async_update_playlist(self, item_id: int, playlist: Playlist):
        """Update a playlist record in the database."""
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = Playlist.from_db_row(
                await self.__execute_fetchone(
                    db_conn, "SELECT * FROM playlists WHERE item_id=?;", (item_id,)
                )
            )
            metadata = merge_dict(cur_item.metadata, playlist.metadata)
            provider_ids = merge_list(cur_item.provider_ids, playlist.provider_ids)
            sql_query = """UPDATE playlists
                SET name=?,
                    sort_name=?,
                    owner=?,
                    is_editable=?,
                    checksum=?,
                    metadata=?,
                    provider_ids=?
                WHERE item_id=?;"""
            await db_conn.execute(
                sql_query,
                (
                    playlist.name,
                    playlist.sort_name,
                    playlist.owner,
                    playlist.is_editable,
                    playlist.checksum,
                    json_serializer(metadata),
                    json_serializer(provider_ids),
                    item_id,
                ),
            )
            await self.__async_add_prov_ids(
                item_id, MediaType.Playlist, playlist.provider_ids, db_conn
            )
            LOGGER.debug("updated playlist %s in database: %s", playlist.name, item_id)
            await db_conn.commit()
        # return updated object
        return await self.async_get_playlist(item_id)

    async def async_add_radio(self, radio: Radio):
        """Add a new radio record to the database."""
        assert radio.name
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = await self.__execute_fetchone(
                db_conn,
                "SELECT (item_id) FROM radios WHERE name=?;",
                (radio.name,),
            )
            if cur_item:
                # update existing
                return await self.async_update_radio(cur_item[0], radio)
            # insert radio
            sql_query = """INSERT INTO radios (name, sort_name, metadata, provider_ids)
                VALUES(?,?,?);"""
            async with db_conn.execute(
                sql_query,
                (
                    radio.name,
                    radio.sort_name,
                    json_serializer(radio.metadata),
                    json_serializer(radio.provider_ids),
                ),
            ) as cursor:
                last_row_id = cursor.lastrowid
                new_item = await self.__execute_fetchone(
                    db_conn,
                    "SELECT (item_id) FROM radios WHERE ROWID=?;",
                    (last_row_id,),
                )
            await self.__async_add_prov_ids(
                new_item[0], MediaType.Radio, radio.provider_ids, db_conn
            )
            await db_conn.commit()
        LOGGER.debug("added radio %s to database", radio.name)
        # return created object
        return await self.async_get_radio(new_item[0])

    async def async_update_radio(self, item_id: int, radio: Radio):
        """Update a radio record in the database."""
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = Radio.from_db_row(
                await self.__execute_fetchone(
                    db_conn, "SELECT * FROM radios WHERE item_id=?;", (item_id,)
                )
            )
            metadata = merge_dict(cur_item.metadata, radio.metadata)
            provider_ids = merge_list(cur_item.provider_ids, radio.provider_ids)
            sql_query = """UPDATE radios
                SET name=?,
                    sort_name=?,
                    metadata=?,
                    provider_ids=?
                WHERE item_id=?;"""
            await db_conn.execute(
                sql_query,
                (
                    radio.name,
                    radio.sort_name,
                    json_serializer(metadata),
                    json_serializer(provider_ids),
                    item_id,
                ),
            )
            await self.__async_add_prov_ids(
                item_id, MediaType.Radio, radio.provider_ids, db_conn
            )
            LOGGER.debug("updated radio %s in database: %s", radio.name, item_id)
            await db_conn.commit()
        # return updated object
        return await self.async_get_radio(item_id)

    async def async_add_to_library(
        self, item_id: int, media_type: MediaType, provider: str
    ):
        """Add an item to the library (item must already be present in the db!)."""
        async with DbConnect(self._dbfile) as db_conn:
            item_id = try_parse_int(item_id)
            db_name = media_type.value + "s"
            sql_query = f"UPDATE {db_name} SET in_library=1 WHERE item_id=?;"
            await db_conn.execute(sql_query, (item_id,))
            await db_conn.commit()

    async def async_remove_from_library(
        self, item_id: int, media_type: MediaType, provider: str
    ):
        """Remove item from the library."""
        async with DbConnect(self._dbfile) as db_conn:
            item_id = try_parse_int(item_id)
            db_name = media_type.value + "s"
            sql_query = f"UPDATE {db_name} SET in_library=0 WHERE item_id=?;"
            await db_conn.execute(sql_query, (item_id,))
            await db_conn.commit()

    async def async_get_artists(
        self,
        filter_query: str = None,
        orderby: str = "name",
        db_conn: aiosqlite.Connection = None,
    ) -> List[Artist]:
        """Fetch artist records from database."""
        sql_query = "SELECT * FROM artists"
        if filter_query:
            sql_query += " " + filter_query
        sql_query += " ORDER BY %s" % orderby
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            return [
                Artist.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def async_get_artist(
        self, item_id: int, db_conn: aiosqlite.Connection = None
    ) -> Artist:
        """Get artist record by id."""
        item_id = try_parse_int(item_id)
        for item in await self.async_get_artists(
            "WHERE item_id = %d" % item_id, db_conn=db_conn
        ):
            return item
        return None

    async def async_add_artist(self, artist: Artist):
        """Add a new artist record to the database."""
        assert artist.musicbrainz_id
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = await self.__execute_fetchone(
                db_conn,
                "SELECT (item_id) FROM artists WHERE musicbrainz_id=?;",
                (artist.musicbrainz_id,),
            )
            if cur_item:
                # update existing
                return await self.async_update_artist(cur_item[0], artist)
            # insert artist
            sql_query = """INSERT INTO artists
                (name, sort_name, musicbrainz_id, metadata, provider_ids)
                VALUES(?,?,?,?,?);"""
            async with db_conn.execute(
                sql_query,
                (
                    artist.name,
                    artist.sort_name,
                    artist.musicbrainz_id,
                    json_serializer(artist.metadata),
                    json_serializer(artist.provider_ids),
                ),
            ) as cursor:
                last_row_id = cursor.lastrowid
                new_item = await self.__execute_fetchone(
                    db_conn,
                    "SELECT (item_id) FROM artists WHERE ROWID=?;",
                    (last_row_id,),
                )
            await self.__async_add_prov_ids(
                new_item[0], MediaType.Artist, artist.provider_ids, db_conn
            )
            await db_conn.commit()
        LOGGER.debug("added artist %s to database", artist.name)
        # return created object
        return await self.async_get_artist(new_item[0])

    async def async_update_artist(self, item_id: int, artist: Artist):
        """Update a artist record in the database."""
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            db_row = await self.__execute_fetchone(
                db_conn, "SELECT * FROM artists WHERE item_id=?;", (item_id,)
            )
            cur_item = Artist.from_db_row(db_row)
            metadata = merge_dict(cur_item.metadata, artist.metadata)
            provider_ids = merge_list(cur_item.provider_ids, artist.provider_ids)
            sql_query = """UPDATE artists
                SET name=?,
                    sort_name=?,
                    musicbrainz_id=?,
                    metadata=?,
                    provider_ids=?
                WHERE item_id=?;"""
            await db_conn.execute(
                sql_query,
                (
                    artist.name,
                    artist.sort_name,
                    artist.musicbrainz_id,
                    json_serializer(metadata),
                    json_serializer(provider_ids),
                    item_id,
                ),
            )
            await self.__async_add_prov_ids(
                item_id, MediaType.Artist, artist.provider_ids, db_conn
            )
            LOGGER.debug("updated artist %s in database: %s", artist.name, item_id)
            await db_conn.commit()
        # return updated object
        return await self.async_get_artist(item_id)

    async def async_get_albums(
        self,
        filter_query: str = None,
        orderby: str = "name",
        db_conn: aiosqlite.Connection = None,
    ) -> List[Album]:
        """Fetch all album records from the database."""
        sql_query = "SELECT * FROM albums"
        if filter_query:
            sql_query += " " + filter_query
        sql_query += " ORDER BY %s" % orderby
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            return [
                Album.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def async_get_album(
        self, item_id: int, db_conn: aiosqlite.Connection = None
    ) -> Album:
        """Get album record by id."""
        item_id = try_parse_int(item_id)
        # get from db
        for item in await self.async_get_albums(
            "WHERE item_id = %d" % item_id, db_conn=db_conn
        ):
            item.artist = await self.async_get_artist(item.artist.item_id)
            return item
        return None

    async def async_add_album(self, album: Album):
        """Add a new album record to the database."""
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row

            # always try to grab existing item by external_id
            cur_item = await self.__execute_fetchone(
                db_conn,
                "SELECT (item_id) FROM albums WHERE upc=?;",
                (album.upc,),
            )
            # fallback to matching on artist, name and version
            if not cur_item:
                cur_item = await self.__execute_fetchone(
                    db_conn,
                    """SELECT item_id FROM albums WHERE
                        json_extract("artist", '$.item_id') = ?
                        AND sort_name=? AND version=? AND year=? AND album_type=?""",
                    (
                        album.artist.item_id,
                        album.sort_name,
                        album.version,
                        int(album.year),
                        album.album_type.value,
                    ),
                )
            # fallback to almost exact match
            if not cur_item:
                for item in await db_conn.execute_fetchall(
                    """SELECT * FROM albums WHERE
                        json_extract("artist", '$.item_id') = ?
                        AND sort_name = ?""",
                    (album.artist.item_id, album.sort_name),
                ):
                    if (not album.version and item["year"] == album.year) or (
                        album.version and item["version"] == album.version
                    ):
                        cur_item = item
                        break

            if cur_item:
                # update existing
                return await self.async_update_album(cur_item[0], album)
            # insert album
            album_artist = AlbumArtist(
                item_id=album.artist.item_id,
                provider="database",
                name=album.artist.name,
            )
            sql_query = """INSERT INTO albums
                (name, sort_name, album_type, year, version, upc, artist, metadata, provider_ids)
                VALUES(?,?,?,?,?,?,?,?,?);"""
            async with db_conn.execute(
                sql_query,
                (
                    album.name,
                    album.sort_name,
                    album.album_type.value,
                    album.year,
                    album.version,
                    album.upc,
                    json_serializer(album_artist),
                    json_serializer(album.metadata),
                    json_serializer(album.provider_ids),
                ),
            ) as cursor:
                last_row_id = cursor.lastrowid
                new_item = await self.__execute_fetchone(
                    db_conn,
                    "SELECT (item_id) FROM albums WHERE ROWID=?;",
                    (last_row_id,),
                )
            await self.__async_add_prov_ids(
                new_item[0], MediaType.Album, album.provider_ids, db_conn
            )
            await db_conn.commit()
        LOGGER.debug("added album %s to database", album.name)
        # return created object
        return await self.async_get_album(new_item[0])

    async def async_update_album(self, item_id: int, album: Album):
        """Update a album record in the database."""
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = Album.from_db_row(
                await self.__execute_fetchone(
                    db_conn, "SELECT * FROM albums WHERE item_id=?;", (item_id,)
                )
            )
            album_artist = AlbumArtist(
                item_id=album.artist.item_id,
                provider="database",
                name=album.artist.name,
            )
            metadata = merge_dict(cur_item.metadata, album.metadata)
            provider_ids = merge_list(cur_item.provider_ids, album.provider_ids)
            sql_query = """UPDATE albums
                SET name=?,
                    sort_name=?,
                    album_type=?,
                    year=?,
                    version=?,
                    upc=?,
                    artist=?,
                    metadata=?,
                    provider_ids=?
                WHERE item_id=?;"""
            await db_conn.execute(
                sql_query,
                (
                    album.name,
                    album.sort_name,
                    album.album_type.value,
                    album.year,
                    album.version,
                    album.upc,
                    json_serializer(album_artist),
                    json_serializer(metadata),
                    json_serializer(provider_ids),
                    item_id,
                ),
            )
            await self.__async_add_prov_ids(
                item_id, MediaType.Album, album.provider_ids, db_conn
            )
            LOGGER.debug("updated album %s in database: %s", album.name, item_id)
            await db_conn.commit()
        # return updated object
        return await self.async_get_album(item_id)

    async def async_get_tracks(
        self,
        filter_query: str = None,
        orderby: str = "name",
        db_conn: aiosqlite.Connection = None,
    ) -> List[Track]:
        """Return all track records from the database."""
        sql_query = "SELECT * FROM tracks"
        if filter_query:
            sql_query += " " + filter_query
        sql_query += " ORDER BY %s" % orderby
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            return [
                Track.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def async_get_tracks_from_provider_ids(
        self,
        provider_id: str,
        prov_item_ids: List[str],
    ) -> dict:
        """Get track records for the given prov_ids."""
        prov_item_id_str = ",".join([f'"{x}"' for x in prov_item_ids])
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE provider = '{provider_id}' AND media_type = 'track'
                AND prov_item_id in ({prov_item_id_str})
            )"""
        return await self.async_get_tracks(sql_query)

    async def async_get_track(
        self, item_id: int, db_conn: aiosqlite.Connection = None
    ) -> Track:
        """Get track record by id."""
        item_id = try_parse_int(item_id)
        for item in await self.async_get_tracks(
            "WHERE item_id = %d" % item_id, db_conn=db_conn
        ):
            item.album = await self.async_get_album(item.album.item_id)
            artist_ids = [str(x.item_id) for x in item.artists]
            query = "WHERE item_id in (%s)" % ",".join(artist_ids)
            item.artists = await self.async_get_artists(query)
            return item
        return None

    async def async_add_track(self, track: Track):
        """Add a new track record to the database."""
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row

            # always try to grab existing item by external_id
            cur_item = await self.__execute_fetchone(
                db_conn,
                "SELECT (item_id) FROM tracks WHERE isrc=?;",
                (track.isrc,),
            )
            # fallback to matching on item_id, name and version
            if not cur_item:
                for item in await db_conn.execute_fetchall(
                    """SELECT * FROM tracks WHERE
                        json_extract("album", '$.item_id') = ?
                        AND sort_name=?""",
                    (
                        track.album.item_id,
                        track.sort_name,
                    ),
                ):
                    # we perform an additional safety check on the duration or version
                    if (
                        track.version
                        and compare_strings(item["version"], track.version)
                    ) or (
                        (
                            not track.version
                            and not item["version"]
                            and abs(item["duration"] - track.duration) < 10
                        )
                    ):
                        cur_item = item
                        break

            if cur_item:
                # update existing
                return await self.async_update_track(cur_item[0], track)
            # insert track
            sql_query = """INSERT INTO tracks
                (name, sort_name, album, artists, duration, version, isrc, metadata, provider_ids)
                VALUES(?,?,?,?,?,?,?,?,?);"""
            # we store a simplified artist/album object in tracks
            artists = [
                TrackArtist(item_id=x.item_id, provider="database", name=x.name)
                for x in track.artists
            ]
            album = TrackAlbum(
                item_id=track.album.item_id, provider="database", name=track.album.name
            )
            async with db_conn.execute(
                sql_query,
                (
                    track.name,
                    track.sort_name,
                    json_serializer(album),
                    json_serializer(artists),
                    track.duration,
                    track.version,
                    track.isrc,
                    json_serializer(track.metadata),
                    json_serializer(track.provider_ids),
                ),
            ) as cursor:
                last_row_id = cursor.lastrowid
                new_item = await self.__execute_fetchone(
                    db_conn,
                    "SELECT (item_id) FROM tracks WHERE ROWID=?;",
                    (last_row_id,),
                )
            await self.__async_add_prov_ids(
                new_item[0], MediaType.Track, track.provider_ids, db_conn
            )
            await db_conn.commit()
        LOGGER.debug("added track %s to database", track.name)
        # return created object
        return await self.async_get_track(new_item[0])

    async def async_update_track(self, item_id: int, track: Track):
        """Update a track record in the database."""
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = Track.from_db_row(
                await self.__execute_fetchone(
                    db_conn, "SELECT * FROM tracks WHERE item_id=?;", (item_id,)
                )
            )
            metadata = merge_dict(cur_item.metadata, track.metadata)
            provider_ids = merge_list(cur_item.provider_ids, track.provider_ids)
            artists = [
                TrackArtist(item_id=x.item_id, provider="database", name=x.name)
                for x in track.artists
            ]
            album = TrackAlbum(
                item_id=track.album.item_id, provider="database", name=track.album.name
            )
            sql_query = """UPDATE tracks
                SET name=?,
                    sort_name=?,
                    album=?,
                    artists=?,
                    duration=?,
                    version=?,
                    isrc=?,
                    metadata=?,
                    provider_ids=?
                WHERE item_id=?;"""
            await db_conn.execute(
                sql_query,
                (
                    track.name,
                    track.sort_name,
                    json_serializer(album),
                    json_serializer(artists),
                    track.duration,
                    track.version,
                    track.isrc,
                    json_serializer(metadata),
                    json_serializer(provider_ids),
                    item_id,
                ),
            )
            await self.__async_add_prov_ids(
                item_id, MediaType.Track, track.provider_ids, db_conn
            )
            LOGGER.debug("updated track %s in database: %s", track.name, item_id)
            await db_conn.commit()
        # return updated object
        return await self.async_get_track(item_id)

    async def async_get_artist_albums(
        self, item_id: int, orderby: str = "name"
    ) -> List[Album]:
        """Get all library albums for the given artist."""
        # TODO: use json query type instead of text search
        sql_query = f"WHERE json_extract(\"artist\", '$.item_id') = {item_id}"
        return await self.async_get_albums(sql_query, orderby=orderby)

    async def async_set_track_loudness(
        self, provider_item_id: str, provider: str, loudness: int
    ):
        """Set integrated loudness for a track in db."""
        async with DbConnect(self._dbfile) as db_conn:
            sql_query = """INSERT or REPLACE INTO track_loudness
                (provider_item_id, provider, loudness) VALUES(?,?,?);"""
            await db_conn.execute(sql_query, (provider_item_id, provider, loudness))
            await db_conn.commit()

    async def async_get_track_loudness(self, provider_item_id, provider):
        """Get integrated loudness for a track in db."""
        async with DbConnect(self._dbfile) as db_conn:
            sql_query = """SELECT loudness FROM track_loudness WHERE
                provider_item_id = ? AND provider = ?"""
            async with db_conn.execute(
                sql_query, (provider_item_id, provider)
            ) as cursor:
                result = await cursor.fetchone()
            if result:
                return result[0]
        return None

    async def __async_add_prov_ids(
        self,
        item_id: int,
        media_type: MediaType,
        provider_ids: List[MediaItemProviderId],
        db_conn: aiosqlite.Connection,
    ):
        """Add provider ids for media item to database."""

        for prov in provider_ids:
            sql_query = """INSERT OR REPLACE INTO provider_mappings
                (item_id, media_type, prov_item_id, provider, quality, details)
                VALUES(?,?,?,?,?,?);"""
            await db_conn.execute(
                sql_query,
                (
                    item_id,
                    media_type.value,
                    prov.item_id,
                    prov.provider,
                    prov.quality,
                    prov.details,
                ),
            )

    async def __execute_fetchone(
        self, db_conn: aiosqlite.Connection, query: str, query_params: tuple
    ):
        """Return first row of given query."""
        for item in await db_conn.execute_fetchall(query, query_params):
            return item
        return None
