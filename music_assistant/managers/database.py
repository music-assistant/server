"""Database logic."""
# pylint: disable=too-many-lines
import logging
import os
import sqlite3
from functools import partial
from typing import List

import aiosqlite
from music_assistant.helpers.util import compare_strings, get_sort_name, try_parse_int
from music_assistant.models.media_types import (
    Album,
    AlbumType,
    Artist,
    ExternalId,
    MediaItem,
    MediaItemProviderId,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    Track,
    TrackQuality,
)

LOGGER = logging.getLogger("database")


class DbConnect:
    """Helper to initialize the db connection or utilize an existing one."""

    def __init__(self, dbfile: str, db_conn: sqlite3.Connection = None):
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
        self._dbfile = os.path.join(mass.config.data_path, "database.db")
        self.db_conn = partial(DbConnect, self._dbfile)

    async def async_setup(self):
        """Async initialization."""
        async with DbConnect(self._dbfile) as db_conn:

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS library_items(
                    item_id INTEGER NOT NULL, provider TEXT NOT NULL,
                    media_type INTEGER NOT NULL, UNIQUE(item_id, provider, media_type)
                );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS artists(
                    artist_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    sort_name TEXT, musicbrainz_id TEXT NOT NULL UNIQUE);"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS albums(
                    album_id INTEGER PRIMARY KEY AUTOINCREMENT, artist_id INTEGER NOT NULL,
                    name TEXT NOT NULL, albumtype TEXT, year INTEGER, version TEXT,
                    UNIQUE(artist_id, name, version, year)
                );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS labels(
                    label_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);"""
            )
            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS album_labels(
                    album_id INTEGER, label_id INTEGER, UNIQUE(album_id, label_id));"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS tracks(
                    track_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    album_id INTEGER, version TEXT, duration INTEGER,
                    UNIQUE(name, version, album_id, duration)
                );"""
            )
            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS track_artists(
                    track_id INTEGER, artist_id INTEGER, UNIQUE(track_id, artist_id));"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS tags(
                    tag_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);"""
            )
            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS media_tags(
                    item_id INTEGER, media_type INTEGER, tag_id,
                    UNIQUE(item_id, media_type, tag_id)
                );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS provider_mappings(
                    item_id INTEGER NOT NULL, media_type INTEGER NOT NULL,
                    prov_item_id TEXT NOT NULL,
                    provider TEXT NOT NULL, quality INTEGER NOT NULL, details TEXT NULL,
                    UNIQUE(item_id, media_type, prov_item_id, provider, quality)
                    );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS metadata(
                    item_id INTEGER NOT NULL, media_type INTEGER NOT NULL, key TEXT NOT NULL,
                    value TEXT, UNIQUE(item_id, media_type, key));"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS external_ids(
                    item_id INTEGER NOT NULL, media_type INTEGER NOT NULL, key TEXT NOT NULL,
                    value TEXT, UNIQUE(item_id, media_type, key, value));"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS playlists(
                    playlist_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    owner TEXT NOT NULL, is_editable BOOLEAN NOT NULL, checksum TEXT NOT NULL,
                    UNIQUE(name, owner)
                    );"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS radios(
                    radio_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);"""
            )

            await db_conn.execute(
                """CREATE TABLE IF NOT EXISTS track_loudness(
                    provider_track_id INTEGER NOT NULL, provider TEXT NOT NULL, loudness REAL,
                    UNIQUE(provider_track_id, provider));"""
            )

            await db_conn.commit()
            await db_conn.execute("VACUUM;")
            await db_conn.commit()

    async def async_get_database_id(
        self,
        provider_id: str,
        prov_item_id: str,
        media_type: MediaType,
        db_conn: sqlite3.Connection = None,
    ) -> int:
        """Get the database id for the given prov_id."""
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            if provider_id == "database":
                return prov_item_id
            sql_query = """SELECT item_id FROM provider_mappings
                WHERE prov_item_id = ? AND provider = ? AND media_type = ?;"""
            async with db_conn.execute(
                sql_query, (prov_item_id, provider_id, int(media_type))
            ) as cursor:
                item_id = await cursor.fetchone()
            if item_id:
                return item_id[0]
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
                result.artists = [
                    item
                    async for item in self.async_get_artists(sql_query, db_conn=db_conn)
                ]
            if media_types is None or MediaType.Album in media_types:
                sql_query = ' WHERE name LIKE "%s"' % searchquery
                result.albums = [
                    item
                    async for item in self.async_get_albums(sql_query, db_conn=db_conn)
                ]
            if media_types is None or MediaType.Track in media_types:
                sql_query = ' WHERE name LIKE "%s"' % searchquery
                result.tracks = [
                    item
                    async for item in self.async_get_tracks(sql_query, db_conn=db_conn)
                ]
            if media_types is None or MediaType.Playlist in media_types:
                sql_query = ' WHERE name LIKE "%s"' % searchquery
                result.playlists = [
                    item
                    async for item in self.async_get_playlists(
                        sql_query, db_conn=db_conn
                    )
                ]
            if media_types is None or MediaType.Radio in media_types:
                sql_query = ' WHERE name LIKE "%s"' % searchquery
                result.radios = [
                    item
                    async for item in self.async_get_radios(sql_query, db_conn=db_conn)
                ]
            return result

    async def async_get_library_artists(
        self, provider_id: str = None, orderby: str = "name"
    ) -> List[Artist]:
        """Get all library artists, optionally filtered by provider."""
        if provider_id is not None:
            sql_query = f"""WHERE artist_id in (SELECT item_id FROM library_items WHERE
                provider = "{provider_id}" AND media_type = {int(MediaType.Artist)})"""
        else:
            sql_query = f"""WHERE artist_id in
                    (SELECT item_id FROM library_items
                    WHERE media_type = {int(MediaType.Artist)})"""
        async for item in self.async_get_artists(
            sql_query, orderby=orderby, fulldata=True
        ):
            yield item

    async def async_get_library_albums(
        self, provider_id: str = None, orderby: str = "name"
    ) -> List[Album]:
        """Get all library albums, optionally filtered by provider."""
        if provider_id is not None:
            sql_query = f"""WHERE album_id in (SELECT item_id FROM library_items
                WHERE provider = "{provider_id}" AND media_type = {int(MediaType.Album)})"""
        else:
            sql_query = f"""WHERE album_id in
                (SELECT item_id FROM library_items WHERE media_type = {int(MediaType.Album)})"""
        async for item in self.async_get_albums(
            sql_query, orderby=orderby, fulldata=True
        ):
            yield item

    async def async_get_library_tracks(
        self, provider_id: str = None, orderby: str = "name"
    ) -> List[Track]:
        """Get all library tracks, optionally filtered by provider."""
        if provider_id is not None:
            sql_query = f"""WHERE track_id in
                (SELECT item_id FROM library_items WHERE provider = "{provider_id}"
                AND media_type = {int(MediaType.Track)})"""
        else:
            sql_query = f"""WHERE track_id in
                (SELECT item_id FROM library_items WHERE media_type = {int(MediaType.Track)})"""
        async for item in self.async_get_tracks(sql_query, orderby=orderby):
            yield item

    async def async_get_library_playlists(
        self, provider_id: str = None, orderby: str = "name"
    ) -> List[Playlist]:
        """Fetch all playlist records from table."""
        if provider_id is not None:
            sql_query = f"""WHERE playlist_id in
                (SELECT item_id FROM library_items WHERE provider = "{provider_id}"
                AND media_type = {int(MediaType.Playlist)})"""
        else:
            sql_query = f"""WHERE playlist_id in
                (SELECT item_id FROM library_items WHERE media_type = {int(MediaType.Playlist)})"""
        async for item in self.async_get_playlists(sql_query, orderby=orderby):
            yield item

    async def async_get_library_radios(
        self, provider_id: str = None, orderby: str = "name"
    ) -> List[Radio]:
        """Fetch all radio records from table."""
        if provider_id is not None:
            sql_query = f"""WHERE radio_id in
                (SELECT item_id FROM library_items WHERE provider = "{provider_id}"
                AND media_type = { int(MediaType.Radio)})"""
        else:
            sql_query = f"""WHERE radio_id in
                (SELECT item_id FROM library_items WHERE media_type = {int(MediaType.Radio)})"""
        async for item in self.async_get_radios(sql_query, orderby=orderby):
            yield item

    async def async_get_playlists(
        self,
        filter_query: str = None,
        orderby: str = "name",
        db_conn: sqlite3.Connection = None,
    ) -> List[Playlist]:
        """Get all playlists from database."""
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            sql_query = "SELECT * FROM playlists"
            if filter_query:
                sql_query += " " + filter_query
            sql_query += " ORDER BY %s" % orderby
            async with db_conn.execute(sql_query) as cursor:
                db_rows = await cursor.fetchall()
            for db_row in db_rows:
                playlist = Playlist(
                    item_id=db_row["playlist_id"],
                    provider="database",
                    name=db_row["name"],
                    metadata=await self.__async_get_metadata(
                        db_row["playlist_id"], MediaType.Playlist, db_conn
                    ),
                    tags=await self.__async_get_tags(
                        db_row["playlist_id"], int(MediaType.Playlist), db_conn
                    ),
                    external_ids=await self.__async_get_external_ids(
                        db_row["playlist_id"], MediaType.Playlist, db_conn
                    ),
                    provider_ids=await self.__async_get_prov_ids(
                        db_row["playlist_id"], MediaType.Playlist, db_conn
                    ),
                    in_library=await self.__async_get_library_providers(
                        db_row["playlist_id"], MediaType.Playlist, db_conn
                    ),
                    is_lazy=False,
                    available=True,
                    owner=db_row["owner"],
                    checksum=db_row["checksum"],
                    is_editable=db_row["is_editable"],
                )
                yield playlist

    async def async_get_playlist(self, playlist_id: int) -> Playlist:
        """Get playlist record by id."""
        playlist_id = try_parse_int(playlist_id)
        async for item in self.async_get_playlists(
            f"WHERE playlist_id = {playlist_id}"
        ):
            return item
        return None

    async def async_get_radios(
        self,
        filter_query: str = None,
        orderby: str = "name",
        db_conn: sqlite3.Connection = None,
    ) -> List[Radio]:
        """Fetch radio records from database."""
        sql_query = "SELECT * FROM radios"
        if filter_query:
            sql_query += " " + filter_query
        sql_query += " ORDER BY %s" % orderby
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            async with db_conn.execute(sql_query) as cursor:
                db_rows = await cursor.fetchall()
            for db_row in db_rows:
                radio = Radio(
                    item_id=db_row["radio_id"],
                    provider="database",
                    name=db_row["name"],
                    metadata=await self.__async_get_metadata(
                        db_row["radio_id"], MediaType.Radio, db_conn
                    ),
                    tags=await self.__async_get_tags(
                        db_row["radio_id"], MediaType.Radio, db_conn
                    ),
                    external_ids=await self.__async_get_external_ids(
                        db_row["radio_id"], MediaType.Radio, db_conn
                    ),
                    provider_ids=await self.__async_get_prov_ids(
                        db_row["radio_id"], MediaType.Radio, db_conn
                    ),
                    in_library=await self.__async_get_library_providers(
                        db_row["radio_id"], MediaType.Radio, db_conn
                    ),
                    is_lazy=False,
                    available=True,
                )
                yield radio

    async def async_get_radio(self, radio_id: int) -> Playlist:
        """Get radio record by id."""
        radio_id = try_parse_int(radio_id)
        async for item in self.async_get_radios(f"WHERE radio_id = {radio_id}"):
            return item
        return None

    async def async_add_playlist(self, playlist: Playlist):
        """Add a new playlist record to the database."""
        assert playlist.name
        async with DbConnect(self._dbfile) as db_conn:
            async with db_conn.execute(
                "SELECT (playlist_id) FROM playlists WHERE name=? AND owner=?;",
                (playlist.name, playlist.owner),
            ) as cursor:
                result = await cursor.fetchone()
            if result:
                playlist_id = result[0]
                # update existing
                sql_query = "UPDATE playlists SET is_editable=?, checksum=? WHERE playlist_id=?;"
                await db_conn.execute(
                    sql_query, (playlist.is_editable, playlist.checksum, playlist_id)
                )
            else:
                # insert playlist
                sql_query = """INSERT INTO playlists (name, owner, is_editable, checksum)
                VALUES(?,?,?,?);"""
                async with db_conn.execute(
                    sql_query,
                    (
                        playlist.name,
                        playlist.owner,
                        playlist.is_editable,
                        playlist.checksum,
                    ),
                ) as cursor:
                    last_row_id = cursor.lastrowid
                # get id from newly created item
                sql_query = "SELECT (playlist_id) FROM playlists WHERE ROWID=?"
                async with db_conn.execute(sql_query, (last_row_id,)) as cursor:
                    playlist_id = await cursor.fetchone()
                    playlist_id = playlist_id[0]
                LOGGER.debug(
                    "added playlist %s to database: %s", playlist.name, playlist_id
                )
            # add/update metadata
            await self.__async_add_prov_ids(
                playlist_id, MediaType.Playlist, playlist.provider_ids, db_conn
            )
            await self.__async_add_metadata(
                playlist_id, MediaType.Playlist, playlist.metadata, db_conn
            )
            # save
            await db_conn.commit()
        return playlist_id

    async def async_add_radio(self, radio: Radio):
        """Add a new radio record to the database."""
        assert radio.name
        async with DbConnect(self._dbfile) as db_conn:
            async with db_conn.execute(
                "SELECT (radio_id) FROM radios WHERE name=?;", (radio.name,)
            ) as cursor:
                result = await cursor.fetchone()
            if result:
                radio_id = result[0]
            else:
                # insert radio
                sql_query = "INSERT INTO radios (name) VALUES(?);"
                async with db_conn.execute(sql_query, (radio.name,)) as cursor:
                    last_row_id = cursor.lastrowid
                    # await db_conn.commit()
                # get id from newly created item
                sql_query = "SELECT (radio_id) FROM radios WHERE ROWID=?"
                async with db_conn.execute(sql_query, (last_row_id,)) as cursor:
                    radio_id = await cursor.fetchone()
                    radio_id = radio_id[0]
                LOGGER.debug(
                    "added radio station %s to database: %s", radio.name, radio_id
                )
            # add/update metadata
            await self.__async_add_prov_ids(
                radio_id, MediaType.Radio, radio.provider_ids, db_conn
            )
            await self.__async_add_metadata(
                radio_id, MediaType.Radio, radio.metadata, db_conn
            )
            # save
            await db_conn.commit()
        return radio_id

    async def async_add_to_library(
        self, item_id: int, media_type: MediaType, provider: str
    ):
        """Add an item to the library (item must already be present in the db!)."""
        async with DbConnect(self._dbfile) as db_conn:
            item_id = try_parse_int(item_id)
            sql_query = """INSERT or REPLACE INTO library_items
                (item_id, provider, media_type) VALUES(?,?,?);"""
            await db_conn.execute(sql_query, (item_id, provider, int(media_type)))
            await db_conn.commit()

    async def async_remove_from_library(
        self, item_id: int, media_type: MediaType, provider: str
    ):
        """Remove item from the library."""
        async with DbConnect(self._dbfile) as db_conn:
            item_id = try_parse_int(item_id)
            sql_query = "DELETE FROM library_items WHERE item_id=? AND provider=? AND media_type=?;"
            await db_conn.execute(sql_query, (item_id, provider, int(media_type)))
            if media_type == MediaType.Playlist:
                sql_query = "DELETE FROM playlists WHERE playlist_id=?;"
                await db_conn.execute(sql_query, (item_id,))
                sql_query = """DELETE FROM provider_mappings WHERE
                    item_id=? AND media_type=? AND provider=?;"""
                await db_conn.execute(sql_query, (item_id, int(media_type), provider))
                await db_conn.commit()

    async def async_get_artists(
        self,
        filter_query: str = None,
        orderby: str = "name",
        fulldata=False,
        db_conn: sqlite3.Connection = None,
    ) -> List[Artist]:
        """Fetch artist records from database."""
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            sql_query = "SELECT * FROM artists"
            if filter_query:
                sql_query += " " + filter_query
            sql_query += " ORDER BY %s" % orderby
            for db_row in await db_conn.execute_fetchall(sql_query):
                artist = Artist(
                    item_id=db_row["artist_id"],
                    provider="database",
                    name=db_row["name"],
                    sort_name=db_row["sort_name"],
                )
                if fulldata:
                    artist.provider_ids = await self.__async_get_prov_ids(
                        db_row["artist_id"], MediaType.Artist, db_conn
                    )
                    artist.in_library = await self.__async_get_library_providers(
                        db_row["artist_id"], MediaType.Artist, db_conn
                    )
                    artist.external_ids = await self.__async_get_external_ids(
                        artist.item_id, MediaType.Artist, db_conn
                    )
                    artist.metadata = await self.__async_get_metadata(
                        artist.item_id, MediaType.Artist, db_conn
                    )
                    artist.tags = await self.__async_get_tags(
                        artist.item_id, MediaType.Artist, db_conn
                    )
                yield artist

    async def async_get_artist(
        self, artist_id: int, fulldata=True, db_conn: sqlite3.Connection = None
    ) -> Artist:
        """Get artist record by id."""
        artist_id = try_parse_int(artist_id)
        async for item in self.async_get_artists(
            "WHERE artist_id = %d" % artist_id, fulldata=fulldata, db_conn=db_conn
        ):
            return item
        return None

    async def async_add_artist(self, artist: Artist) -> int:
        """Add a new artist record to the database."""
        artist_id = None
        async with DbConnect(self._dbfile) as db_conn:
            # always prefer to grab existing artist with external_id (=musicbrainz_id)
            artist_id = await self.__async_get_item_by_external_id(artist, db_conn)
            if not artist_id:
                # insert artist
                musicbrainz_id = artist.external_ids.get(ExternalId.MUSICBRAINZ)
                assert musicbrainz_id  # musicbrainz id is required
                if not artist.sort_name:
                    artist.sort_name = get_sort_name(artist.name)
                sql_query = "INSERT INTO artists (name, sort_name, musicbrainz_id) VALUES(?,?,?);"
                async with db_conn.execute(
                    sql_query, (artist.name, artist.sort_name, musicbrainz_id)
                ) as cursor:
                    last_row_id = cursor.lastrowid
                await db_conn.commit()
                # get id from (newly created) item
                async with db_conn.execute(
                    "SELECT artist_id FROM artists WHERE ROWID=?;", (last_row_id,)
                ) as cursor:
                    artist_id = await cursor.fetchone()
                    artist_id = artist_id[0]
            # always add metadata and tags etc. because we might have received
            # additional info or a match from other provider
            await self.__async_add_prov_ids(
                artist_id, MediaType.Artist, artist.provider_ids, db_conn
            )
            await self.__async_add_metadata(
                artist_id, MediaType.Artist, artist.metadata, db_conn
            )
            await self.__async_add_tags(
                artist_id, MediaType.Artist, artist.tags, db_conn
            )
            await self.__async_add_external_ids(
                artist_id, MediaType.Artist, artist.external_ids, db_conn
            )
            # save
            await db_conn.commit()
            LOGGER.debug(
                "added artist %s (%s) to database: %s",
                artist.name,
                artist.provider_ids,
                artist_id,
            )
        return artist_id

    async def async_get_albums(
        self,
        filter_query: str = None,
        orderby: str = "name",
        fulldata=False,
        db_conn: sqlite3.Connection = None,
    ) -> List[Album]:
        """Fetch all album records from the database."""
        sql_query = "SELECT * FROM albums"
        if filter_query:
            sql_query += " " + filter_query
        sql_query += " ORDER BY %s" % orderby
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            for db_row in await db_conn.execute_fetchall(sql_query):
                album = Album(
                    item_id=db_row["album_id"],
                    provider="database",
                    name=db_row["name"],
                    album_type=AlbumType(int(db_row["albumtype"])),
                    year=db_row["year"],
                    version=db_row["version"],
                    artist=await self.async_get_artist(
                        db_row["artist_id"], fulldata=fulldata, db_conn=db_conn
                    ),
                )
                if fulldata:
                    album.provider_ids = await self.__async_get_prov_ids(
                        db_row["album_id"], MediaType.Album, db_conn
                    )
                    album.in_library = await self.__async_get_library_providers(
                        db_row["album_id"], MediaType.Album, db_conn
                    )
                    album.external_ids = await self.__async_get_external_ids(
                        album.item_id, MediaType.Album, db_conn
                    )
                    album.metadata = await self.__async_get_metadata(
                        album.item_id, MediaType.Album, db_conn
                    )
                    album.tags = await self.__async_get_tags(
                        album.item_id, MediaType.Album, db_conn
                    )
                    album.labels = await self.__async_get_album_labels(
                        album.item_id, db_conn
                    )
                yield album

    async def async_get_album(
        self, album_id: int, fulldata=True, db_conn: sqlite3.Connection = None
    ) -> Album:
        """Get album record by id."""
        album_id = try_parse_int(album_id)
        async for item in self.async_get_albums(
            "WHERE album_id = %d" % album_id, fulldata=fulldata, db_conn=db_conn
        ):
            return item
        return None

    async def async_add_album(self, album: Album) -> int:
        """Add a new album record to the database."""
        assert album.name and album.artist
        assert album.artist.provider == "database"
        album_id = None
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            # always try to grab existing album with external_id
            album_id = await self.__async_get_item_by_external_id(album, db_conn)
            # fallback to matching on artist_id, name and version
            if not album_id:
                sql_query = """SELECT album_id FROM albums WHERE
                    artist_id=? AND name=? AND version=? AND year=? AND albumtype=?"""
                async with db_conn.execute(
                    sql_query,
                    (
                        album.artist.item_id,
                        album.name,
                        album.version,
                        int(album.year),
                        int(album.album_type),
                    ),
                ) as cursor:
                    res = await cursor.fetchone()
                    if res:
                        album_id = res["album_id"]
            # fallback to almost exact match
            if not album_id:
                sql_query = """SELECT album_id, year, version, albumtype FROM
                    albums WHERE artist_id=? AND name=?"""
                async with db_conn.execute(
                    sql_query, (album.artist.item_id, album.name)
                ) as cursor:
                    albums = await cursor.fetchall()
                for result in albums:
                    if (not album.version and result["year"] == album.year) or (
                        album.version and result["version"] == album.version
                    ):
                        album_id = result["album_id"]
                        break
            # no match: insert album
            if not album_id:
                sql_query = """INSERT INTO albums (artist_id, name, albumtype, year, version)
                    VALUES(?,?,?,?,?);"""
                query_params = (
                    album.artist.item_id,
                    album.name,
                    int(album.album_type),
                    album.year,
                    album.version,
                )
                async with db_conn.execute(sql_query, query_params) as cursor:
                    last_row_id = cursor.lastrowid
                # get id from newly created item
                sql_query = "SELECT (album_id) FROM albums WHERE ROWID=?"
                async with db_conn.execute(sql_query, (last_row_id,)) as cursor:
                    album_id = await cursor.fetchone()
                    album_id = album_id[0]
                await db_conn.commit()
            # always add metadata and tags etc. because we might have received
            # additional info or a match from other provider
            await self.__async_add_prov_ids(
                album_id, MediaType.Album, album.provider_ids, db_conn
            )
            await self.__async_add_metadata(
                album_id, MediaType.Album, album.metadata, db_conn
            )
            await self.__async_add_tags(album_id, MediaType.Album, album.tags, db_conn)
            await self.__async_add_album_labels(album_id, album.labels, db_conn)
            await self.__async_add_external_ids(
                album_id, MediaType.Album, album.external_ids, db_conn
            )
            # save
            await db_conn.commit()
            LOGGER.debug(
                "added album %s (%s) to database: %s",
                album.name,
                album.provider_ids,
                album_id,
            )
        return album_id

    async def async_get_tracks(
        self,
        filter_query: str = None,
        orderby: str = "name",
        fulldata=False,
        db_conn: sqlite3.Connection = None,
    ) -> List[Track]:
        """Return all track records from the database."""
        async with DbConnect(self._dbfile, db_conn) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            sql_query = "SELECT * FROM tracks"
            if filter_query:
                sql_query += " " + filter_query
            sql_query += " ORDER BY %s" % orderby
            for db_row in await db_conn.execute_fetchall(sql_query, ()):
                track = Track(
                    item_id=db_row["track_id"],
                    provider="database",
                    name=db_row["name"],
                    external_ids=await self.__async_get_external_ids(
                        db_row["track_id"], MediaType.Track, db_conn
                    ),
                    provider_ids=await self.__async_get_prov_ids(
                        db_row["track_id"], MediaType.Track, db_conn
                    ),
                    in_library=await self.__async_get_library_providers(
                        db_row["track_id"], MediaType.Track, db_conn
                    ),
                    duration=db_row["duration"],
                    version=db_row["version"],
                    album=await self.async_get_album(
                        db_row["album_id"], fulldata=fulldata, db_conn=db_conn
                    ),
                    artists=await self.__async_get_track_artists(
                        db_row["track_id"], db_conn=db_conn, fulldata=fulldata
                    ),
                )
                if fulldata:
                    track.metadata = await self.__async_get_metadata(
                        db_row["track_id"], MediaType.Track, db_conn
                    )
                    track.tags = await self.__async_get_tags(
                        db_row["track_id"], MediaType.Track, db_conn
                    )
                yield track

    async def async_get_track(
        self, track_id: int, fulldata=True, db_conn: sqlite3.Connection = None
    ) -> Track:
        """Get track record by id."""
        track_id = try_parse_int(track_id)
        async for item in self.async_get_tracks(
            "WHERE track_id = %d" % track_id, fulldata=fulldata, db_conn=db_conn
        ):
            return item
        return None

    async def async_add_track(self, track: Track) -> int:
        """Add a new track record to the database."""
        assert track.name and track.album
        assert track.album.provider == "database"
        assert track.artists
        for artist in track.artists:
            assert artist.provider == "database"
        async with DbConnect(self._dbfile) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            # always try to grab existing track with external_id
            track_id = await self.__async_get_item_by_external_id(track, db_conn)
            # fallback to matching on album_id, name and version
            if not track_id:
                sql_query = "SELECT track_id, duration, version \
                    FROM tracks WHERE album_id=? AND name=?"
                async with db_conn.execute(
                    sql_query, (track.album.item_id, track.name)
                ) as cursor:
                    results = await cursor.fetchall()
                for result in results:
                    # we perform an additional safety check on the duration or version
                    if (
                        track.version
                        and compare_strings(result["version"], track.version)
                    ) or (
                        (
                            not track.version
                            and not result["version"]
                            and abs(result["duration"] - track.duration) < 10
                        )
                    ):
                        track_id = result["track_id"]
                        break
            # no match found: insert track
            if not track_id:
                assert track.name and track.album.item_id
                sql_query = "INSERT INTO tracks (name, album_id, duration, version) \
                        VALUES(?,?,?,?);"
                query_params = (
                    track.name,
                    track.album.item_id,
                    track.duration,
                    track.version,
                )
                async with db_conn.execute(sql_query, query_params) as cursor:
                    last_row_id = cursor.lastrowid
                await db_conn.commit()
                # get id from newly created item (the safe way)
                async with db_conn.execute(
                    "SELECT track_id FROM tracks WHERE ROWID=?", (last_row_id,)
                ) as cursor:
                    track_id = await cursor.fetchone()
                    track_id = track_id[0]
            # always add metadata and tags etc. because we might have received
            # additional info or a match from other provider
            for artist in track.artists:
                sql_query = "INSERT or IGNORE INTO track_artists (track_id, artist_id) VALUES(?,?);"
                await db_conn.execute(sql_query, (track_id, artist.item_id))
            await self.__async_add_prov_ids(
                track_id, MediaType.Track, track.provider_ids, db_conn
            )
            await self.__async_add_metadata(
                track_id, MediaType.Track, track.metadata, db_conn
            )
            await self.__async_add_tags(track_id, MediaType.Track, track.tags, db_conn)
            await self.__async_add_external_ids(
                track_id, MediaType.Track, track.external_ids, db_conn
            )
            # save to db
            await db_conn.commit()
            LOGGER.debug(
                "added track %s (%s) to database: %s",
                track.name,
                track.provider_ids,
                track_id,
            )
        return track_id

    async def async_update_playlist(
        self, playlist_id: int, column_key: str, column_value: str
    ):
        """Update column of existing playlist."""
        async with DbConnect(self._dbfile) as db_conn:
            sql_query = f"UPDATE playlists SET {column_key}=? WHERE playlist_id=?;"
            await db_conn.execute(sql_query, (column_value, playlist_id))
            await db_conn.commit()

    async def async_get_artist_tracks(
        self, artist_id: int, orderby: str = "name"
    ) -> List[Track]:
        """Get all library tracks for the given artist."""
        artist_id = try_parse_int(artist_id)
        sql_query = f"""WHERE track_id in
            (SELECT track_id FROM track_artists WHERE artist_id = {artist_id})"""
        async for item in self.async_get_tracks(
            sql_query, orderby=orderby, fulldata=False
        ):
            yield item

    async def async_get_artist_albums(
        self, artist_id: int, orderby: str = "name"
    ) -> List[Album]:
        """Get all library albums for the given artist."""
        sql_query = " WHERE artist_id = %s" % artist_id
        async for item in self.async_get_albums(
            sql_query, orderby=orderby, fulldata=False
        ):
            yield item

    async def async_set_track_loudness(
        self, provider_track_id: str, provider: str, loudness: int
    ):
        """Set integrated loudness for a track in db."""
        async with DbConnect(self._dbfile) as db_conn:
            sql_query = """INSERT or REPLACE INTO track_loudness
                (provider_track_id, provider, loudness) VALUES(?,?,?);"""
            await db_conn.execute(sql_query, (provider_track_id, provider, loudness))
            await db_conn.commit()

    async def async_get_track_loudness(self, provider_track_id, provider):
        """Get integrated loudness for a track in db."""
        async with DbConnect(self._dbfile) as db_conn:
            sql_query = """SELECT loudness FROM track_loudness WHERE
                provider_track_id = ? AND provider = ?"""
            async with db_conn.execute(
                sql_query, (provider_track_id, provider)
            ) as cursor:
                result = await cursor.fetchone()
            if result:
                return result[0]
        return None

    async def __async_add_metadata(
        self,
        item_id: int,
        media_type: MediaType,
        metadata: dict,
        db_conn: sqlite3.Connection,
    ):
        """Add or update metadata."""
        for key, value in metadata.items():
            if value:
                sql_query = """INSERT or REPLACE INTO metadata
                    (item_id, media_type, key, value) VALUES(?,?,?,?);"""
                await db_conn.execute(sql_query, (item_id, int(media_type), key, value))

    async def __async_get_metadata(
        self,
        item_id: int,
        media_type: MediaType,
        db_conn: sqlite3.Connection,
        filter_key: str = None,
    ) -> dict:
        """Get metadata for media item."""
        metadata = {}
        sql_query = (
            "SELECT key, value FROM metadata WHERE item_id = ? AND media_type = ?"
        )
        if filter_key:
            sql_query += ' AND key = "%s"' % filter_key
        async with db_conn.execute(sql_query, (item_id, int(media_type))) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            key = db_row[0]
            value = db_row[1]
            metadata[key] = value
        return metadata

    async def __async_add_tags(
        self,
        item_id: int,
        media_type: MediaType,
        tags: List[str],
        db_conn: sqlite3.Connection,
    ):
        """Add tags to db."""
        for tag in tags:
            sql_query = "INSERT or IGNORE INTO tags (name) VALUES(?);"
            async with db_conn.execute(sql_query, (tag,)) as cursor:
                tag_id = cursor.lastrowid
            sql_query = """INSERT or IGNORE INTO media_tags
                (item_id, media_type, tag_id) VALUES(?,?,?);"""
            await db_conn.execute(sql_query, (item_id, int(media_type), tag_id))

    async def __async_get_tags(
        self, item_id: int, media_type: MediaType, db_conn: sqlite3.Connection
    ) -> List[str]:
        """Get tags for media item."""
        tags = []
        sql_query = """SELECT name FROM tags INNER JOIN media_tags ON
            tags.tag_id = media_tags.tag_id WHERE item_id = ? AND media_type = ?"""
        async with db_conn.execute(sql_query, (item_id, int(media_type))) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            tags.append(db_row[0])
        return tags

    async def __async_add_album_labels(
        self, album_id: int, labels: List[str], db_conn: sqlite3.Connection
    ):
        """Add labels to album in db."""
        for label in labels:
            sql_query = "INSERT or IGNORE INTO labels (name) VALUES(?);"
            async with db_conn.execute(sql_query, (label,)) as cursor:
                label_id = cursor.lastrowid
            sql_query = (
                "INSERT or IGNORE INTO album_labels (album_id, label_id) VALUES(?,?);"
            )
            await db_conn.execute(sql_query, (album_id, label_id))

    async def __async_get_album_labels(
        self, album_id: int, db_conn: sqlite3.Connection
    ) -> List[str]:
        """Get labels for album item."""
        labels = []
        sql_query = """SELECT name FROM labels INNER JOIN album_labels
            ON labels.label_id = album_labels.label_id WHERE album_id = ?"""
        async with db_conn.execute(sql_query, (album_id,)) as cursor:
            db_rows = await cursor.fetchall()
        for db_row in db_rows:
            labels.append(db_row[0])
        return labels

    async def __async_get_track_artists(
        self, track_id: int, db_conn: sqlite3.Connection, fulldata: bool = False
    ) -> List[Artist]:
        """Get artists for track."""
        sql_query = (
            "WHERE artist_id in (SELECT artist_id FROM track_artists WHERE track_id = %s)"
            % track_id
        )
        return [
            item
            async for item in self.async_get_artists(
                sql_query, fulldata=fulldata, db_conn=db_conn
            )
        ]

    async def __async_add_external_ids(
        self,
        item_id: int,
        media_type: MediaType,
        external_ids: dict,
        db_conn: sqlite3.Connection,
    ):
        """Add or update external_ids."""
        for key, value in external_ids.items():
            sql_query = """INSERT or REPLACE INTO external_ids
                (item_id, media_type, key, value) VALUES(?,?,?,?);"""
            await db_conn.execute(
                sql_query, (item_id, int(media_type), str(key), value)
            )

    async def __async_get_external_ids(
        self, item_id: int, media_type: MediaType, db_conn: sqlite3.Connection
    ) -> dict:
        """Get external_ids for media item."""
        external_ids = {}
        sql_query = (
            "SELECT key, value FROM external_ids WHERE item_id = ? AND media_type = ?"
        )
        for db_row in await db_conn.execute_fetchall(
            sql_query, (item_id, int(media_type))
        ):
            external_ids[db_row[0]] = db_row[1]
        return external_ids

    async def __async_add_prov_ids(
        self,
        item_id: int,
        media_type: MediaType,
        provider_ids: List[MediaItemProviderId],
        db_conn: sqlite3.Connection,
    ):
        """Add provider ids for media item to db_conn."""

        for prov in provider_ids:
            sql_query = """INSERT OR REPLACE INTO provider_mappings
                (item_id, media_type, prov_item_id, provider, quality, details)
                VALUES(?,?,?,?,?,?);"""
            await db_conn.execute(
                sql_query,
                (
                    item_id,
                    int(media_type),
                    prov.item_id,
                    prov.provider,
                    int(prov.quality),
                    prov.details,
                ),
            )

    async def __async_get_prov_ids(
        self, item_id: int, media_type: MediaType, db_conn: sqlite3.Connection
    ) -> List[MediaItemProviderId]:
        """Get all provider id's for media item."""
        provider_ids = []
        sql_query = "SELECT prov_item_id, provider, quality, details \
            FROM provider_mappings \
            WHERE item_id = ? AND media_type = ?"
        for db_row in await db_conn.execute_fetchall(
            sql_query, (item_id, int(media_type))
        ):
            prov_mapping = MediaItemProviderId(
                provider=db_row["provider"],
                item_id=db_row["prov_item_id"],
                quality=TrackQuality(db_row["quality"]),
                details=db_row["details"],
            )
            provider_ids.append(prov_mapping)
        return provider_ids

    async def __async_get_library_providers(
        self, db_item_id: int, media_type: MediaType, db_conn: sqlite3.Connection
    ) -> List[str]:
        """Get the providers that have this media_item added to the library."""
        providers = []
        sql_query = (
            "SELECT provider FROM library_items WHERE item_id = ? AND media_type = ?"
        )
        for db_row in await db_conn.execute_fetchall(
            sql_query, (db_item_id, int(media_type))
        ):
            providers.append(db_row[0])
        return providers

    async def __async_get_item_by_external_id(
        self, media_item: MediaItem, db_conn: sqlite3.Connection
    ) -> int:
        """Try to get existing item in db by matching the new item's external id's."""
        for key, value in media_item.external_ids.items():
            sql_query = "SELECT (item_id) FROM external_ids \
                    WHERE media_type=? AND key=? AND value=?;"
            for db_row in await db_conn.execute_fetchall(
                sql_query, (int(media_item.media_type), str(key), value)
            ):
                if db_row:
                    return db_row[0]
        return None
