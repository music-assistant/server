"""Read-only access to a beets library database."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiosqlite

from .constants import (
    BEETS_LIST_DELIMITER,
    BEETS_MULTI_VALUE_DELIMITER,
    ITEM_BATCH_SIZE,
    SQLITE_BUSY_TIMEOUT,
)

_TABLES = ("items", "albums", "item_attributes", "album_attributes")
# album artists are looked up before track artists
_SINGLE_ARTIST_LOOKUPS = (
    ("albums", "albumartist", "albumartist_sort", "mb_albumartistid"),
    ("items", "artist", "artist_sort", "mb_artistid"),
)
_LIST_ARTIST_LOOKUPS = (
    ("albums", "albumartists", "albumartists_sort", "mb_albumartistids"),
    ("items", "artists", "artists_sort", "mb_artistids"),
)
# a small prefilter cap: the exact match below still runs over every candidate row
_ARTIST_LIST_LOOKUP_LIMIT = 20
_LIKE_ESCAPE_CHAR = "\\"


def split_multi_value(value: object) -> list[str]:
    """
    Split a beets multi-valued field into its values, keeping empty positions.

    :param value: The raw database value.
    """
    if not isinstance(value, str) or not value:
        return []
    if BEETS_MULTI_VALUE_DELIMITER in value:
        return value.split(BEETS_MULTI_VALUE_DELIMITER)
    return value.split(BEETS_LIST_DELIMITER)


def value_at(values: list[str], index: int) -> str | None:
    """Return the stripped value at index, or None when it is missing or empty."""
    if index >= len(values):
        return None
    return values[index].strip() or None


class BeetsLibraryError(Exception):
    """Raised when the beets library database cannot be opened or read."""


@dataclass
class BeetsRow:
    """A row of the beets items or albums table plus its flexible attributes."""

    id: int
    fields: dict[str, Any]
    flex: dict[str, Any] = field(default_factory=dict)

    @property
    def album_id(self) -> int | None:
        """Return the album id of an item row, or None for singletons and album rows."""
        value = self.fields.get("album_id")
        return int(value) if value else None


class BeetsLibrary:
    """Read-only reader for a beets library.db."""

    def __init__(self, db_path: str) -> None:
        """
        Initialize the reader.

        :param db_path: Path to the beets library database.
        """
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._columns: dict[str, frozenset[str]] = {}

    async def open(self) -> None:
        """
        Open the database read-only.

        :raises BeetsLibraryError: If the file cannot be opened or holds no beets items table.
        """
        await self.close()
        try:
            self._db = await aiosqlite.connect(
                f"file:{quote(self.db_path)}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT
            )
            self._db.row_factory = aiosqlite.Row
            columns = {table: await self._table_columns(table) for table in _TABLES}
        except BeetsLibraryError:
            # already reports its own reason (from _fetch_all); do not wrap it again
            await self.close()
            raise
        except sqlite3.Error as err:
            await self.close()
            msg = f"Unable to open {self.db_path}: {err}"
            raise BeetsLibraryError(msg) from err
        if not columns["items"]:
            await self.close()
            msg = f"{self.db_path} has no beets items table"
            raise BeetsLibraryError(msg)
        self._columns = columns

    async def close(self) -> None:
        """Close the database connection if it is open."""
        if self._db is not None:
            await self._db.close()
            self._db = None
        self._columns = {}

    async def count_items(self) -> int:
        """Return the number of items in the library."""
        rows = await self._fetch_all("SELECT COUNT(*) AS total FROM items")
        return int(rows[0]["total"])

    async def get_albums(self) -> dict[int, BeetsRow]:
        """Return every album keyed by its beets id."""
        self._require_open()
        if not self._columns.get("albums"):
            return {}
        rows = await self._fetch_all("SELECT * FROM albums")
        return {row.id: row for row in await self._with_flex(rows, "album_attributes")}

    async def iter_items(self, batch_size: int = ITEM_BATCH_SIZE) -> AsyncGenerator[list[BeetsRow]]:
        """
        Yield all items in batches ordered by id.

        No lock is held on the database between batches, so beets can keep writing to it.

        :param batch_size: Maximum number of items per batch.
        """
        last_id = 0
        while True:
            rows = await self._fetch_all(
                "SELECT * FROM items WHERE id > ? ORDER BY id LIMIT ?", (last_id, batch_size)
            )
            if not rows:
                return
            yield await self._with_flex(rows, "item_attributes")
            last_id = int(rows[-1]["id"])

    async def get_item(self, item_id: int) -> BeetsRow | None:
        """
        Return one item, or None when beets has no item with this id.

        :param item_id: The beets item id.
        """
        rows = await self._fetch_all("SELECT * FROM items WHERE id = ?", (item_id,))
        return next(iter(await self._with_flex(rows, "item_attributes")), None)

    async def get_album(self, album_id: int) -> BeetsRow | None:
        """
        Return one album, or None when beets has no album with this id.

        :param album_id: The beets album id.
        """
        self._require_open()
        if not self._columns.get("albums"):
            return None
        rows = await self._fetch_all("SELECT * FROM albums WHERE id = ?", (album_id,))
        return next(iter(await self._with_flex(rows, "album_attributes")), None)

    async def get_album_items(self, album_id: int) -> list[BeetsRow]:
        """
        Return the items of an album in disc and track order.

        :param album_id: The beets album id.
        """
        rows = await self._fetch_all(
            "SELECT * FROM items WHERE album_id = ? ORDER BY disc, track, id", (album_id,)
        )
        return await self._with_flex(rows, "item_attributes")

    async def get_artist_details(self, name: str) -> tuple[str | None, str | None] | None:
        """
        Return the sort name and MusicBrainz id beets holds for an artist name.

        Album artists are looked up before track artists, exact single-valued columns before
        the multi-valued lists (so a featured artist is also found). Returns None when no album
        or item carries this artist name.

        :param name: The artist name.
        """
        self._require_open()
        for table, name_column, sort_column, mbid_column in _SINGLE_ARTIST_LOOKUPS:
            columns = self._columns.get(table, frozenset())
            if name_column not in columns:
                continue
            sort_expr = sort_column if sort_column in columns else "NULL"
            mbid_expr = mbid_column if mbid_column in columns else "NULL"
            rows = await self._fetch_all(
                f"SELECT {sort_expr} AS sort_name, {mbid_expr} AS mbid "
                f"FROM {table} WHERE {name_column} = ? LIMIT 1",
                (name,),
            )
            if rows:
                return (rows[0]["sort_name"] or None, rows[0]["mbid"] or None)
        for table, list_column, sort_column, mbid_column in _LIST_ARTIST_LOOKUPS:
            columns = self._columns.get(table, frozenset())
            if list_column not in columns:
                continue
            sort_expr = sort_column if sort_column in columns else "NULL"
            mbid_expr = mbid_column if mbid_column in columns else "NULL"
            rows = await self._fetch_all(
                f"SELECT {list_column} AS names, {sort_expr} AS sort_names, "
                f"{mbid_expr} AS mbids FROM {table} WHERE {list_column} LIKE ? "
                f"ESCAPE '{_LIKE_ESCAPE_CHAR}' LIMIT ?",
                (_like_pattern(name), _ARTIST_LIST_LOOKUP_LIMIT),
            )
            for row in rows:
                if (index := _index_of(split_multi_value(row["names"]), name)) is None:
                    continue
                sort_name = value_at(split_multi_value(row["sort_names"]), index)
                mbid = value_at(split_multi_value(row["mbids"]), index)
                return (sort_name, mbid)
        return None

    def _require_open(self) -> aiosqlite.Connection:
        """
        Return the open database connection.

        :raises BeetsLibraryError: If the database connection is not open.
        """
        if self._db is None:
            msg = "The beets library is not open"
            raise BeetsLibraryError(msg)
        return self._db

    async def _table_columns(self, table: str) -> frozenset[str]:
        rows = await self._fetch_all(f"PRAGMA table_info({table})")
        return frozenset(str(row["name"]) for row in rows)

    async def _with_flex(self, rows: list[sqlite3.Row], flex_table: str) -> list[BeetsRow]:
        result = [BeetsRow(id=int(row["id"]), fields=dict(row)) for row in rows]
        if not result or not self._columns.get(flex_table):
            return result
        by_id = {row.id: row for row in result}
        ids = list(by_id)
        # chunked so a large album list stays below SQLite's bound-parameter limit
        for start in range(0, len(ids), ITEM_BATCH_SIZE):
            chunk = ids[start : start + ITEM_BATCH_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            attributes = await self._fetch_all(
                f"SELECT entity_id, key, value FROM {flex_table} "
                f"WHERE entity_id IN ({placeholders})",
                chunk,
            )
            for attribute in attributes:
                if (row := by_id.get(int(attribute["entity_id"]))) is not None:
                    row.flex[str(attribute["key"])] = attribute["value"]
        return result

    async def _fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        db = self._require_open()
        try:
            return list(await db.execute_fetchall(sql, tuple(params)))
        except sqlite3.Error as err:
            msg = f"Unable to read {self.db_path}: {err}"
            raise BeetsLibraryError(msg) from err


def _like_pattern(name: str) -> str:
    """Return a SQL LIKE pattern that matches name as a literal substring."""
    escaped = name
    for char in (_LIKE_ESCAPE_CHAR, "%", "_"):
        escaped = escaped.replace(char, f"{_LIKE_ESCAPE_CHAR}{char}")
    return f"%{escaped}%"


def _index_of(values: list[str], name: str) -> int | None:
    """Return the index of the element that strips to an exact match of name, or None."""
    for index, value in enumerate(values):
        if value.strip() == name:
            return index
    return None
