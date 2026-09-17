"""Build beets-shaped SQLite library databases for the beets provider tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MULTI_VALUE_DELIMITER = "\\␀"

ARTIST_MBID = "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"
GUEST_MBID = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
ALBUM_MBID = "1dc4c347-a1db-32aa-b14f-bc9cc507b843"
RELEASE_GROUP_MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
TRACK_MBID = "b1a9c0e9-d987-4042-ae91-78d6a3267d69"

_ITEM_COLUMNS = (
    "path BLOB",
    "album_id INTEGER",
    "title TEXT",
    "artist TEXT",
    "artist_sort TEXT",
    "mb_artistid TEXT",
    "artists TEXT",
    "artists_sort TEXT",
    "mb_artistids TEXT",
    "albumartist TEXT",
    "album TEXT",
    "genres TEXT",
    "style TEXT",
    "grouping TEXT",
    "lyrics TEXT",
    "label TEXT",
    "comments TEXT",
    "year INTEGER",
    "month INTEGER",
    "day INTEGER",
    "track INTEGER",
    "disc INTEGER",
    "length REAL",
    "bitrate INTEGER",
    "samplerate INTEGER",
    "bitdepth INTEGER",
    "channels INTEGER",
    "format TEXT",
    "mb_trackid TEXT",
    "isrc TEXT",
    "acoustid_id TEXT",
    "added REAL",
    "mtime REAL",
    "rg_track_gain REAL",
    "rg_album_gain REAL",
    "r128_track_gain REAL",
    "r128_album_gain REAL",
)
_ALBUM_COLUMNS = (
    "artpath BLOB",
    "album TEXT",
    "albumartist TEXT",
    "albumartist_sort TEXT",
    "mb_albumartistid TEXT",
    "albumartists TEXT",
    "albumartists_sort TEXT",
    "mb_albumartistids TEXT",
    "albumtype TEXT",
    "albumtypes TEXT",
    "comp INTEGER",
    "year INTEGER",
    "original_year INTEGER",
    "mb_albumid TEXT",
    "mb_releasegroupid TEXT",
    "barcode TEXT",
    "asin TEXT",
    "label TEXT",
    "genres TEXT",
)
# older beets databases predate the multi-value columns and hold a single genre string
_LEGACY_ONLY_SINGLE = {"artists", "artists_sort", "mb_artistids", "genres"}
_LEGACY_ALBUM_ONLY_SINGLE = {"albumartists", "albumartists_sort", "mb_albumartistids", "genres"}
_LEGACY_ITEM_COLUMNS = (
    *(column for column in _ITEM_COLUMNS if column.split()[0] not in _LEGACY_ONLY_SINGLE),
    "genre TEXT",
)
_LEGACY_ALBUM_COLUMNS = (
    *(column for column in _ALBUM_COLUMNS if column.split()[0] not in _LEGACY_ALBUM_ONLY_SINGLE),
    "genre TEXT",
)


def album_fields(**overrides: Any) -> dict[str, Any]:
    """
    Return the fields of a typical current-schema beets album row.

    :param overrides: Fields to replace or add.
    """
    return {
        "album": "Album",
        "albumartist": "Artist",
        "albumartist_sort": "Artist",
        "mb_albumartistid": ARTIST_MBID,
        "albumartists": "Artist",
        "albumartists_sort": "Artist",
        "mb_albumartistids": ARTIST_MBID,
        "albumtype": "album",
        "albumtypes": "album",
        "comp": 0,
        "year": 2001,
        "original_year": 1999,
        "mb_albumid": ALBUM_MBID,
        "mb_releasegroupid": RELEASE_GROUP_MBID,
        "barcode": "0123456789012",
        "asin": "B000002UAL",
        "label": "Label",
        "genres": f"Rock{MULTI_VALUE_DELIMITER}Indie",
        **overrides,
    }


def item_fields(**overrides: Any) -> dict[str, Any]:
    """
    Return the fields of a typical current-schema beets item row.

    :param overrides: Fields to replace or add.
    """
    return {
        "path": b"Artist/Album/01 Song.flac",
        "title": "Song",
        "artist": "Artist feat. Guest",
        "artist_sort": "Artist feat. Guest",
        "mb_artistid": ARTIST_MBID,
        "artists": f"Artist{MULTI_VALUE_DELIMITER}Guest",
        "artists_sort": f"Artist{MULTI_VALUE_DELIMITER}Guest",
        "mb_artistids": f"{ARTIST_MBID}{MULTI_VALUE_DELIMITER}{GUEST_MBID}",
        "albumartist": "Artist",
        "album": "Album",
        "genres": f"Rock{MULTI_VALUE_DELIMITER}Indie",
        "style": "Shoegaze",
        "grouping": "Side A",
        "lyrics": "la la la",
        "label": "Label",
        "comments": "A comment",
        "year": 2001,
        "month": 5,
        "day": 7,
        "track": 1,
        "disc": 1,
        "length": 215.4,
        "bitrate": 1011000,
        "samplerate": 44100,
        "bitdepth": 16,
        "channels": 2,
        "format": "FLAC",
        "mb_trackid": TRACK_MBID,
        "isrc": "USRC17607839",
        "acoustid_id": "9ff5a4e3-0a8b-4d1b-9d7e-0f1d6b9f1c11",
        "added": 1700000000.5,
        "mtime": 1700000000.0,
        **overrides,
    }


class BeetsDb:
    """A beets-shaped library database on disk that tests can fill and edit."""

    def __init__(self, path: Path, legacy: bool = False) -> None:
        """
        Create the database file with beets' tables.

        :param path: Where to create the database.
        :param legacy: Use the columns of a beets database from before the multi-value fields.
        """
        self.path = path
        item_columns = _LEGACY_ITEM_COLUMNS if legacy else _ITEM_COLUMNS
        album_columns = _LEGACY_ALBUM_COLUMNS if legacy else _ALBUM_COLUMNS
        self._execute(f"CREATE TABLE items (id INTEGER PRIMARY KEY, {', '.join(item_columns)})")
        self._execute(f"CREATE TABLE albums (id INTEGER PRIMARY KEY, {', '.join(album_columns)})")
        for table in ("item_attributes", "album_attributes"):
            self._execute(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, entity_id INTEGER, key TEXT, "
                "value TEXT, UNIQUE(entity_id, key) ON CONFLICT REPLACE)"
            )

    def add_album(self, **fields: Any) -> int:
        """Insert an album row and return its id."""
        return self._insert("albums", fields)

    def add_item(self, **fields: Any) -> int:
        """Insert an item row and return its id."""
        return self._insert("items", fields)

    def update_album(self, album_id: int, **fields: Any) -> None:
        """Change fields of an album row."""
        self._update("albums", album_id, fields)

    def update_item(self, item_id: int, **fields: Any) -> None:
        """Change fields of an item row."""
        self._update("items", item_id, fields)

    def delete_item(self, item_id: int) -> None:
        """Delete an item row."""
        self._execute("DELETE FROM items WHERE id = ?", (item_id,))

    def delete_album(self, album_id: int) -> None:
        """Delete an album row together with its items, the way `beet remove -a` does."""
        self._execute("DELETE FROM items WHERE album_id = ?", (album_id,))
        self._execute("DELETE FROM albums WHERE id = ?", (album_id,))

    def set_item_flex(self, item_id: int, key: str, value: Any) -> None:
        """Set a flexible attribute on an item."""
        self._execute(
            "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)",
            (item_id, key, value),
        )

    def set_album_flex(self, album_id: int, key: str, value: Any) -> None:
        """Set a flexible attribute on an album."""
        self._execute(
            "INSERT INTO album_attributes (entity_id, key, value) VALUES (?, ?, ?)",
            (album_id, key, value),
        )

    def _insert(self, table: str, fields: dict[str, Any]) -> int:
        if not fields:
            return self._execute(f"INSERT INTO {table} DEFAULT VALUES")
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        return self._execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(fields.values())
        )

    def _update(self, table: str, row_id: int, fields: dict[str, Any]) -> None:
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self._execute(f"UPDATE {table} SET {assignments} WHERE id = ?", (*fields.values(), row_id))

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        connection = sqlite3.connect(self.path)
        try:
            cursor = connection.execute(sql, params)
            connection.commit()
            return int(cursor.lastrowid or 0)
        finally:
            connection.close()
