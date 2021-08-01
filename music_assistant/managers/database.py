"""Database logic."""
# pylint: disable=too-many-lines
import logging
import os
import statistics
from typing import List, Optional, Set, Union

import aiosqlite
from music_assistant.helpers.compare import compare_album, compare_track
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.util import merge_dict, merge_list, try_parse_int
from music_assistant.helpers.web import json_serializer
from music_assistant.models.media_types import (
    Album,
    AlbumType,
    Artist,
    FullAlbum,
    FullTrack,
    ItemMapping,
    MediaItem,
    MediaItemProviderId,
    MediaType,
    Playlist,
    Radio,
    SearchResult,
    Track,
)

LOGGER = logging.getLogger("database")


class DatabaseManager:
    """Class that holds the (logic to the) database."""

    def __init__(self, mass):
        """Initialize class."""
        self.mass = mass
        self._dbfile = os.path.join(mass.config.data_path, "music_assistant.db")

    @property
    def db_file(self):
        """Return location of database on disk."""
        return self._dbfile

    async def get_item_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
        media_type: MediaType,
    ) -> Optional[MediaItem]:
        """Get the database item for the given prov_id."""
        if media_type == MediaType.ARTIST:
            return await self.get_artist_by_prov_id(provider_id, prov_item_id)
        if media_type == MediaType.ALBUM:
            return await self.get_album_by_prov_id(provider_id, prov_item_id)
        if media_type == MediaType.TRACK:
            return await self.get_track_by_prov_id(provider_id, prov_item_id)
        if media_type == MediaType.PLAYLIST:
            return await self.get_playlist_by_prov_id(provider_id, prov_item_id)
        if media_type == MediaType.RADIO:
            return await self.get_radio_by_prov_id(provider_id, prov_item_id)
        return None

    async def get_track_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
    ) -> Optional[FullTrack]:
        """Get the database track for the given prov_id."""
        if provider_id == "database":
            return await self.get_track(prov_item_id)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'track')"""
        for item in await self.get_tracks(sql_query):
            return item
        return None

    async def get_album_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
    ) -> Optional[FullAlbum]:
        """Get the database album for the given prov_id."""
        if provider_id == "database":
            return await self.get_album(prov_item_id)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'album')"""
        for item in await self.get_albums(sql_query):
            return item
        return None

    async def get_artist_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
    ) -> Optional[Artist]:
        """Get the database artist for the given prov_id."""
        if provider_id == "database":
            return await self.get_artist(prov_item_id)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'artist')"""
        for item in await self.get_artists(sql_query):
            return item
        return None

    async def get_playlist_by_prov_id(
        self, provider_id: str, prov_item_id: str
    ) -> Optional[Playlist]:
        """Get the database playlist for the given prov_id."""
        if provider_id == "database":
            return await self.get_playlist(prov_item_id)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'playlist')"""
        for item in await self.get_playlists(sql_query):
            return item
        return None

    async def get_radio_by_prov_id(
        self,
        provider_id: str,
        prov_item_id: str,
    ) -> Optional[Radio]:
        """Get the database radio for the given prov_id."""
        if provider_id == "database":
            return await self.get_radio(prov_item_id)
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE prov_item_id = '{prov_item_id}'
                AND provider = '{provider_id}' AND media_type = 'radio')"""
        for item in await self.get_radios(sql_query):
            return item
        return None

    async def search(
        self, searchquery: str, media_types: List[MediaType]
    ) -> SearchResult:
        """Search library for the given searchphrase."""
        result = SearchResult([], [], [], [], [])
        searchquery = "%" + searchquery + "%"
        if media_types is None or MediaType.ARTIST in media_types:
            sql_query = ' WHERE name LIKE "%s"' % searchquery
            result.artists = await self.get_artists(sql_query)
        if media_types is None or MediaType.ALBUM in media_types:
            sql_query = ' WHERE name LIKE "%s"' % searchquery
            result.albums = await self.get_albums(sql_query)
        if media_types is None or MediaType.TRACK in media_types:
            sql_query = ' WHERE name LIKE "%s"' % searchquery
            result.tracks = await self.get_tracks(sql_query)
        if media_types is None or MediaType.PLAYLIST in media_types:
            sql_query = ' WHERE name LIKE "%s"' % searchquery
            result.playlists = await self.get_playlists(sql_query)
        if media_types is None or MediaType.RADIO in media_types:
            sql_query = ' WHERE name LIKE "%s"' % searchquery
            result.radios = await self.get_radios(sql_query)
        return result

    async def get_library_artists(self, orderby: str = "name") -> List[Artist]:
        """Get all library artists."""
        sql_query = "WHERE in_library = 1"
        return await self.get_artists(sql_query, orderby=orderby)

    async def get_library_albums(self, orderby: str = "name") -> List[Album]:
        """Get all library albums."""
        sql_query = "WHERE in_library = 1"
        return await self.get_albums(sql_query, orderby=orderby)

    async def get_library_tracks(self, orderby: str = "name") -> List[Track]:
        """Get all library tracks."""
        sql_query = "WHERE in_library = 1"
        return await self.get_tracks(sql_query, orderby=orderby)

    async def get_library_playlists(self, orderby: str = "name") -> List[Playlist]:
        """Fetch all playlist records from table."""
        sql_query = "WHERE in_library = 1"
        return await self.get_playlists(sql_query, orderby=orderby)

    async def get_library_radios(
        self, provider_id: str = None, orderby: str = "name"
    ) -> List[Radio]:
        """Fetch all radio records from table."""
        sql_query = "WHERE in_library = 1"
        return await self.get_radios(sql_query, orderby=orderby)

    async def get_playlists(
        self,
        filter_query: str = None,
        orderby: str = "name",
    ) -> List[Playlist]:
        """Get all playlists from database."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            sql_query = "SELECT * FROM playlists"
            if filter_query:
                sql_query += " " + filter_query
            sql_query += " ORDER BY %s" % orderby
            return [
                Playlist.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def get_playlist(self, item_id: int) -> Playlist:
        """Get playlist record by id."""
        item_id = try_parse_int(item_id)
        for item in await self.get_playlists(f"WHERE item_id = {item_id}"):
            return item
        return None

    async def get_radios(
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
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            return [
                Radio.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def get_radio(self, item_id: int) -> Playlist:
        """Get radio record by id."""
        item_id = try_parse_int(item_id)
        for item in await self.get_radios(f"WHERE item_id = {item_id}"):
            return item
        return None

    async def add_playlist(self, playlist: Playlist):
        """Add a new playlist record to the database."""
        assert playlist.name
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = await self.__execute_fetchone(
                "SELECT (item_id) FROM playlists WHERE name=? AND owner=?;",
                (playlist.name, playlist.owner),
                db_conn,
            )

            if cur_item:
                # update existing
                return await self.update_playlist(cur_item[0], playlist)
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
                    "SELECT (item_id) FROM playlists WHERE ROWID=?;",
                    (last_row_id,),
                    db_conn,
                )
            await self._add_prov_ids(
                new_item[0], MediaType.PLAYLIST, playlist.provider_ids, db_conn=db_conn
            )
            await db_conn.commit()
        LOGGER.debug("added playlist %s to database", playlist.name)
        # return created object
        return await self.get_playlist(new_item[0])

    async def update_playlist(self, item_id: int, playlist: Playlist):
        """Update a playlist record in the database."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = Playlist.from_db_row(
                await self.__execute_fetchone(
                    "SELECT * FROM playlists WHERE item_id=?;", (item_id,), db_conn
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
            await self._add_prov_ids(
                item_id, MediaType.PLAYLIST, playlist.provider_ids, db_conn=db_conn
            )
            LOGGER.debug("updated playlist %s in database: %s", playlist.name, item_id)
            await db_conn.commit()
        # return updated object
        return await self.get_playlist(item_id)

    async def add_radio(self, radio: Radio):
        """Add a new radio record to the database."""
        assert radio.name
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = await self.__execute_fetchone(
                "SELECT (item_id) FROM radios WHERE name=?;", (radio.name,), db_conn
            )
            if cur_item:
                # update existing
                return await self.update_radio(cur_item[0], radio)
            # insert radio
            sql_query = """INSERT INTO radios (name, sort_name, metadata, provider_ids)
                VALUES(?,?,?,?);"""
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
                    "SELECT (item_id) FROM radios WHERE ROWID=?;",
                    (last_row_id,),
                    db_conn,
                )
            await self._add_prov_ids(
                new_item[0], MediaType.RADIO, radio.provider_ids, db_conn=db_conn
            )
            await db_conn.commit()
        LOGGER.debug("added radio %s to database", radio.name)
        # return created object
        return await self.get_radio(new_item[0])

    async def update_radio(self, item_id: int, radio: Radio):
        """Update a radio record in the database."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = Radio.from_db_row(
                await self.__execute_fetchone(
                    "SELECT * FROM radios WHERE item_id=?;", (item_id,), db_conn
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
            await self._add_prov_ids(
                item_id, MediaType.RADIO, radio.provider_ids, db_conn=db_conn
            )
            LOGGER.debug("updated radio %s in database: %s", radio.name, item_id)
            await db_conn.commit()
        # return updated object
        return await self.get_radio(item_id)

    async def add_to_library(self, item_id: int, media_type: MediaType):
        """Add an item to the library (item must already be present in the db!)."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            item_id = try_parse_int(item_id)
            db_name = media_type.value + "s"
            sql_query = f"UPDATE {db_name} SET in_library=1 WHERE item_id=?;"
            await db_conn.execute(sql_query, (item_id,))
            await db_conn.commit()

    async def remove_from_library(
        self,
        item_id: int,
        media_type: MediaType,
    ):
        """Remove item from the library."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            item_id = try_parse_int(item_id)
            db_name = media_type.value + "s"
            sql_query = f"UPDATE {db_name} SET in_library=0 WHERE item_id=?;"
            await db_conn.execute(sql_query, (item_id,))
            await db_conn.commit()

    async def get_artists(
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
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            return [
                Artist.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def get_artist(self, item_id: int) -> Artist:
        """Get artist record by id."""
        item_id = try_parse_int(item_id)
        for item in await self.get_artists("WHERE item_id = %d" % item_id):
            return item
        return None

    async def add_artist(self, artist: Artist):
        """Add a new artist record to the database."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = await self.__execute_fetchone(
                "SELECT (item_id) FROM artists WHERE musicbrainz_id=?;",
                (artist.musicbrainz_id,),
                db_conn,
            )
            if cur_item:
                # update existing
                return await self.update_artist(cur_item[0], artist)
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
                    "SELECT (item_id) FROM artists WHERE ROWID=?;",
                    (last_row_id,),
                    db_conn,
                )
            await self._add_prov_ids(
                new_item[0], MediaType.ARTIST, artist.provider_ids, db_conn=db_conn
            )
            await db_conn.commit()
            LOGGER.debug("added artist %s to database", artist.name)
            # return created object
            return await self.get_artist(new_item[0])

    async def update_artist(self, item_id: int, artist: Artist):
        """Update a artist record in the database."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            db_row = await self.__execute_fetchone(
                "SELECT * FROM artists WHERE item_id=?;", (item_id,), db_conn
            )
            cur_item = Artist.from_db_row(db_row)
            metadata = merge_dict(cur_item.metadata, artist.metadata)
            provider_ids = merge_list(cur_item.provider_ids, artist.provider_ids)
            sql_query = """UPDATE artists
                SET musicbrainz_id=?,
                    metadata=?,
                    provider_ids=?
                WHERE item_id=?;"""
            await db_conn.execute(
                sql_query,
                (
                    artist.musicbrainz_id or cur_item.musicbrainz_id,
                    json_serializer(metadata),
                    json_serializer(provider_ids),
                    item_id,
                ),
            )
            await self._add_prov_ids(
                item_id, MediaType.ARTIST, artist.provider_ids, db_conn=db_conn
            )
            LOGGER.debug("updated artist %s in database: %s", artist.name, item_id)
            await db_conn.commit()
            # return updated object
            return await self.get_artist(item_id)

    async def get_albums(
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
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            return [
                Album.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def get_album(self, item_id: int) -> FullAlbum:
        """Get album record by id."""
        item_id = try_parse_int(item_id)
        # get from db
        for item in await self.get_albums("WHERE item_id = %d" % item_id):
            item.artist = (
                await self.get_artist_by_prov_id(
                    item.artist.provider, item.artist.item_id
                )
                or item.artist
            )
            return item
        return None

    async def get_albums_from_provider_ids(
        self, provider_id: Union[str, List[str]], prov_item_ids: List[str]
    ) -> dict:
        """Get album records for the given prov_ids."""
        provider_ids = provider_id if isinstance(provider_id, list) else [provider_id]
        prov_id_str = ",".join([f'"{x}"' for x in provider_ids])
        prov_item_id_str = ",".join([f'"{x}"' for x in prov_item_ids])
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE provider in ({prov_id_str}) AND media_type = 'album'
                AND prov_item_id in ({prov_item_id_str})
            )"""
        return await self.get_albums(sql_query)

    async def add_album(self, album: Album):
        """Add a new album record to the database."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = None
            # always try to grab existing item by external_id
            if album.upc:
                for item in await self.get_albums(f"WHERE upc='{album.upc}'"):
                    cur_item = item
            # fallback to matching
            if not cur_item:
                sql_query = "SELECT item_id from albums WHERE sort_name LIKE ?"
                for db_row in await db_conn.execute_fetchall(
                    sql_query, (album.sort_name,)
                ):
                    item = await self.get_album(db_row["item_id"])
                    if compare_album(item, album):
                        cur_item = item
                        break
            if cur_item:
                # update existing
                return await self.update_album(cur_item.item_id, album)

            # insert album
            assert album.artist
            album_artist = ItemMapping.from_item(
                await self.get_artist_by_prov_id(
                    album.artist.provider, album.artist.item_id
                )
                or album.artist
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
                    "SELECT (item_id) FROM albums WHERE ROWID=?;",
                    (last_row_id,),
                    db_conn,
                )
            await self._add_prov_ids(
                new_item[0], MediaType.ALBUM, album.provider_ids, db_conn=db_conn
            )
            await db_conn.commit()
            LOGGER.debug("added album %s to database", album.name)
            # return created object
            return await self.get_album(new_item[0])

    async def update_album(self, item_id: int, album: Album):
        """Update a album record in the database."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = await self.get_album(item_id)
            album_artist = ItemMapping.from_item(
                await self.get_artist_by_prov_id(
                    cur_item.artist.provider, cur_item.artist.item_id
                )
                or await self.get_artist_by_prov_id(
                    album.artist.provider, album.artist.item_id
                )
                or cur_item.artist
            )
            metadata = merge_dict(cur_item.metadata, album.metadata)
            provider_ids = merge_list(cur_item.provider_ids, album.provider_ids)
            if cur_item.album_type == AlbumType.UNKNOWN:
                album_type = album.album_type
            else:
                album_type = cur_item.album_type
            sql_query = """UPDATE albums
                SET upc=?,
                    artist=?,
                    metadata=?,
                    provider_ids=?,
                    album_type=?
                WHERE item_id=?;"""
            await db_conn.execute(
                sql_query,
                (
                    album.upc or cur_item.upc,
                    json_serializer(album_artist),
                    json_serializer(metadata),
                    json_serializer(provider_ids),
                    album_type.value,
                    item_id,
                ),
            )
            await self._add_prov_ids(
                item_id, MediaType.ALBUM, album.provider_ids, db_conn=db_conn
            )
            LOGGER.debug("updated album %s in database: %s", album.name, item_id)
            await db_conn.commit()
            # return updated object
            return await self.get_album(item_id)

    async def get_tracks(
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
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            return [
                Track.from_db_row(db_row)
                for db_row in await db_conn.execute_fetchall(sql_query, ())
            ]

    async def get_tracks_from_provider_ids(
        self,
        provider_id: Union[str, List[str], Set[str]],
        prov_item_ids: Union[List[str], Set[str]],
    ) -> List[Track]:
        """Get track records for the given prov_ids."""
        provider_ids = provider_id if isinstance(provider_id, list) else [provider_id]
        prov_id_str = ",".join([f'"{x}"' for x in provider_ids])
        prov_item_id_str = ",".join([f'"{x}"' for x in prov_item_ids])
        sql_query = f"""WHERE item_id in
            (SELECT item_id FROM provider_mappings
                WHERE provider in ({prov_id_str}) AND media_type = 'track'
                AND prov_item_id in ({prov_item_id_str})
            )"""
        return await self.get_tracks(sql_query)

    async def get_track(self, item_id: int) -> FullTrack:
        """Get full track record by id."""
        item_id = try_parse_int(item_id)
        for item in await self.get_tracks("WHERE item_id = %d" % item_id):
            # include full album info
            item.albums = set(
                filter(
                    None,
                    [
                        await self.get_album_by_prov_id(album.provider, album.item_id)
                        for album in item.albums
                    ],
                )
            )
            item.album = next(iter(item.albums))
            # include full artist info
            item.artists = {
                await self.get_artist_by_prov_id(artist.provider, artist.item_id)
                or artist
                for artist in item.artists
            }
            return item
        return None

    async def add_track(self, track: Track):
        """Add a new track record to the database."""
        assert track.album, "Track is missing album"
        assert track.artists, "Track is missing artist(s)"
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = None
            # always try to grab existing item by matching
            if track.isrc:
                for item in await self.get_tracks(f"WHERE isrc='{track.isrc}'"):
                    cur_item = item
            # fallback to matching
            if not cur_item:
                sql_query = "SELECT item_id FROM tracks WHERE sort_name LIKE ?"
                for db_row in await db_conn.execute_fetchall(
                    sql_query, (track.sort_name,)
                ):
                    item = await self.get_track(db_row["item_id"])
                    if compare_track(item, track):
                        cur_item = item
                        break
            if cur_item:
                # update existing
                return await self.update_track(cur_item.item_id, track)
            # Item does not yet exist: Insert track
            sql_query = """INSERT INTO tracks
                (name, sort_name, albums, artists, duration, version, isrc, metadata, provider_ids)
                VALUES(?,?,?,?,?,?,?,?,?);"""
            # we store a mapping to artists and albums on the track for easier access/listings
            track_artists = await self._get_track_artists(track)
            track_albums = await self._get_track_albums(track)

            async with db_conn.execute(
                sql_query,
                (
                    track.name,
                    track.sort_name,
                    json_serializer(track_albums),
                    json_serializer(track_artists),
                    track.duration,
                    track.version,
                    track.isrc,
                    json_serializer(track.metadata),
                    json_serializer(track.provider_ids),
                ),
            ) as cursor:
                last_row_id = cursor.lastrowid
                new_item = await self.__execute_fetchone(
                    "SELECT (item_id) FROM tracks WHERE ROWID=?;",
                    (last_row_id,),
                    db_conn,
                )
            await self._add_prov_ids(
                new_item[0], MediaType.TRACK, track.provider_ids, db_conn=db_conn
            )
            await db_conn.commit()
            LOGGER.debug("added track %s to database", track.name)
            # return created object
            return await self.get_track(new_item[0])

    async def update_track(self, item_id: int, track: Track):
        """Update a track record in the database."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            db_conn.row_factory = aiosqlite.Row
            cur_item = await self.get_track(item_id)

            # we store a mapping to artists and albums on the track for easier access/listings
            track_artists = await self._get_track_artists(track, cur_item.artists)
            track_albums = await self._get_track_albums(track, cur_item.albums)
            # merge metadata and provider id's
            metadata = merge_dict(cur_item.metadata, track.metadata)
            provider_ids = merge_list(cur_item.provider_ids, track.provider_ids)
            sql_query = """UPDATE tracks
                SET isrc=?,
                    metadata=?,
                    provider_ids=?,
                    artists=?,
                    albums=?
                WHERE item_id=?;"""
            await db_conn.execute(
                sql_query,
                (
                    track.isrc or cur_item.isrc,
                    json_serializer(metadata),
                    json_serializer(provider_ids),
                    json_serializer(track_artists),
                    json_serializer(track_albums),
                    item_id,
                ),
            )
            await self._add_prov_ids(
                item_id, MediaType.TRACK, track.provider_ids, db_conn=db_conn
            )
            LOGGER.debug("updated track %s in database: %s", track.name, item_id)
            await db_conn.commit()
            # return updated object
            return await self.get_track(item_id)

    async def set_track_loudness(self, item_id: str, provider: str, loudness: int):
        """Set integrated loudness for a track in db."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            sql_query = """INSERT or REPLACE INTO track_loudness
                (item_id, provider, loudness) VALUES(?,?,?);"""
            await db_conn.execute(sql_query, (item_id, provider, loudness))
            await db_conn.commit()

    async def get_track_loudness(self, provider_item_id, provider):
        """Get integrated loudness for a track in db."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            sql_query = """SELECT loudness FROM track_loudness WHERE
                item_id = ? AND provider = ?"""
            async with db_conn.execute(
                sql_query, (provider_item_id, provider)
            ) as cursor:
                result = await cursor.fetchone()
            if result:
                return result[0]
        return None

    async def get_provider_loudness(self, provider) -> Optional[float]:
        """Get average integrated loudness for tracks of given provider."""
        all_items = []
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            sql_query = """SELECT loudness FROM track_loudness WHERE provider = ?"""
            async with db_conn.execute(sql_query, (provider,)) as cursor:
                result = await cursor.fetchone()
            if result:
                return result[0]
        sql_query = """SELECT loudness FROM track_loudness WHERE provider = ?"""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            for db_row in await db_conn.execute_fetchall(sql_query, (provider,)):
                all_items.append(db_row[0])
        if all_items:
            return statistics.fmean(all_items)
        return None

    async def mark_item_played(self, item_id: str, provider: str):
        """Mark item as played in playlog."""
        timestamp = utc_timestamp()
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            sql_query = """INSERT or REPLACE INTO playlog
                (item_id, provider, timestamp) VALUES(?,?,?);"""
            await db_conn.execute(sql_query, (item_id, provider, timestamp))
            await db_conn.commit()

    async def get_thumbnail_id(self, url, size):
        """Get/create id for thumbnail."""
        async with aiosqlite.connect(self._dbfile, timeout=360) as db_conn:
            sql_query = """SELECT id FROM thumbs WHERE
                url = ? AND size = ?"""
            async with db_conn.execute(sql_query, (url, size)) as cursor:
                result = await cursor.fetchone()
            if result:
                return result[0]
            # create if it doesnt exist
            sql_query = """INSERT INTO thumbs
                (url, size) VALUES(?,?);"""
            async with db_conn.execute(
                sql_query,
                (url, size),
            ) as cursor:
                last_row_id = cursor.lastrowid
                new_item = await self.__execute_fetchone(
                    "SELECT id FROM thumbs WHERE ROWID=?;", (last_row_id,), db_conn
                )
            await db_conn.commit()
            return new_item[0]

    async def _add_prov_ids(
        self,
        item_id: int,
        media_type: MediaType,
        provider_ids: Set[MediaItemProviderId],
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
        self, query: str, query_params: tuple, db_conn: aiosqlite.Connection
    ):
        """Return first row of given query."""
        for item in await db_conn.execute_fetchall(query, query_params):
            return item
        return None

    async def _get_track_albums(
        self, track: Track, cur_albums: Optional[Set[ItemMapping]] = None
    ) -> Set[ItemMapping]:
        """Extract all (unique) albums of track as ItemMapping."""
        if not track.albums:
            track.albums.add(track.album)
        if cur_albums is None:
            cur_albums = set()
        cur_albums.update(track.albums)
        track_albums = set()
        for album in cur_albums:
            cur_ids = {x.item_id for x in track_albums}
            if isinstance(album, ItemMapping):
                track_album = await self.get_album_by_prov_id(album.provider_id, album)
            else:
                track_album = await self.add_album(album)
            if track_album.item_id not in cur_ids:
                track_albums.add(ItemMapping.from_item(album))
        return track_albums

    async def _get_track_artists(
        self, track: Track, cur_artists: Optional[Set[ItemMapping]] = None
    ) -> Set[ItemMapping]:
        """Extract all (unique) artists of track as ItemMapping."""
        if cur_artists is None:
            cur_artists = set()
        cur_artists.update(track.artists)
        track_artists = set()
        for item in cur_artists:
            cur_names = {x.name for x in track_artists}
            cur_ids = {x.item_id for x in track_artists}
            track_artist = (
                await self.get_artist_by_prov_id(item.provider, item.item_id) or item
            )
            if (
                track_artist.name not in cur_names
                and track_artist.item_id not in cur_ids
            ):
                track_artists.add(ItemMapping.from_item(track_artist))
        return track_artists
