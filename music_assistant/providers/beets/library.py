"""Read-only access to a beets library database."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiosqlite
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.helpers.tags import clean_mbid

from .constants import (
    BEETS_LIST_DELIMITER,
    BEETS_MULTI_VALUE_DELIMITER,
    ITEM_BATCH_SIZE,
    SQLITE_BUSY_TIMEOUT,
)

# databases from older beets versions lack some columns, such as the multi-valued fields
_COLUMN_CHECKED_TABLES = ("items", "albums")
# album artists are looked up before track artists and, like the parsers read them, the
# multi-valued lists before the single-valued columns: (table, list columns, single columns)
_ARTIST_LOOKUPS = (
    (
        "albums",
        ("albumartists", "albumartists_sort", "mb_albumartistids"),
        ("albumartist", "albumartist_sort", "mb_albumartistid"),
    ),
    (
        "items",
        ("artists", "artists_sort", "mb_artistids"),
        ("artist", "artist_sort", "mb_artistid"),
    ),
)
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


@dataclass(frozen=True)
class BeetsArtist:
    """An artist as beets records it on an album or item."""

    name: str
    sort_name: str | None
    mbid: str | None


@dataclass
class BeetsRow:
    """A row of the beets items or albums table plus its flexible attributes."""

    id: int
    fields: dict[str, Any]
    flex: dict[str, Any] = field(default_factory=dict)
    # size and modification time of an album's cover file, so a replaced cover is noticed
    art_stamp: str | None = None

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

        :raises ProviderUnavailableError: If the database cannot be opened or read.
        """
        await self.close()
        try:
            self._db = await aiosqlite.connect(
                f"file:{quote(self.db_path)}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT
            )
            self._db.row_factory = aiosqlite.Row
            self._columns = {
                table: await self._table_columns(table) for table in _COLUMN_CHECKED_TABLES
            }
        except ProviderUnavailableError:
            # already reports its own reason (from _fetch_all); do not wrap it again
            await self.close()
            raise
        except sqlite3.Error as err:
            await self.close()
            msg = f"Unable to open {self.db_path}: {err}"
            raise ProviderUnavailableError(msg) from err

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

    async def find_artist(
        self, name: str | None = None, mbid: str | None = None
    ) -> BeetsArtist | None:
        """
        Return the first artist beets records with a MusicBrainz id, or with a name and no id.

        Album artists are searched before track artists. Names are compared the way the
        parsers read them, without surrounding whitespace, and an invalid MusicBrainz id
        counts as none. Returns None when no album or item carries such an artist.

        :param name: The artist name, used when mbid is not given.
        :param mbid: The artist's MusicBrainz id in canonical form.
        """
        if mbid is None and not name:
            return None
        for table, list_columns, single_columns in _ARTIST_LOOKUPS:
            columns = self._columns.get(table, frozenset())
            for lookup, multi_valued in ((list_columns, True), (single_columns, False)):
                if artist := await self._find_artist_in(
                    table, columns, lookup, multi_valued, name, mbid
                ):
                    return artist
        return None

    async def _find_artist_in(
        self,
        table: str,
        columns: frozenset[str],
        lookup: tuple[str, str, str],
        multi_valued: bool,
        name: str | None,
        mbid: str | None,
    ) -> BeetsArtist | None:
        """Return the first matching artist in one set of artist columns of a table."""
        name_column, sort_column, mbid_column = lookup
        key_column = mbid_column if mbid else name_column
        if name_column not in columns or key_column not in columns:
            return None
        sort_expr = sort_column if sort_column in columns else "NULL"
        mbid_expr = mbid_column if mbid_column in columns else "NULL"
        if mbid or multi_valued:
            # LIKE only prefilters substrings (case-insensitively), so every candidate is
            # checked for an exact element match below
            condition = f"{key_column} LIKE ? ESCAPE '{_LIKE_ESCAPE_CHAR}'"
            value = _like_pattern(mbid or name or "")
        else:
            condition = f"TRIM({name_column}) = ?"
            value = name or ""
        last_id = 0
        while rows := await self._fetch_all(
            f"SELECT id, {name_column} AS names, {sort_expr} AS sort_names, "
            f"{mbid_expr} AS mbids FROM {table} WHERE {condition} AND id > ? "
            "ORDER BY id LIMIT ?",
            (value, last_id, ITEM_BATCH_SIZE),
        ):
            for row in rows:
                if artist := _matching_artist(row, multi_valued, name, mbid):
                    return artist
            last_id = int(rows[-1]["id"])
        return None

    async def _table_columns(self, table: str) -> frozenset[str]:
        rows = await self._fetch_all(f"PRAGMA table_info({table})")
        return frozenset(str(row["name"]) for row in rows)

    async def _with_flex(self, rows: list[sqlite3.Row], flex_table: str) -> list[BeetsRow]:
        result = [BeetsRow(id=int(row["id"]), fields=dict(row)) for row in rows]
        if not result:
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
        if self._db is None:
            msg = f"The beets library {self.db_path} is not open"
            raise ProviderUnavailableError(msg)
        try:
            return list(await self._db.execute_fetchall(sql, tuple(params)))
        except sqlite3.Error as err:
            msg = f"Unable to read {self.db_path}: {err}"
            raise ProviderUnavailableError(msg) from err


def _like_pattern(name: str) -> str:
    """Return a SQL LIKE pattern that matches name as a literal substring."""
    escaped = name
    for char in (_LIKE_ESCAPE_CHAR, "%", "_"):
        escaped = escaped.replace(char, f"{_LIKE_ESCAPE_CHAR}{char}")
    return f"%{escaped}%"


def _matching_artist(
    row: sqlite3.Row, multi_valued: bool, name: str | None, mbid: str | None
) -> BeetsArtist | None:
    """Return the artist of a row with this MusicBrainz id, or with this name and no id."""
    names = _column_values(row["names"], multi_valued)
    sort_names = _column_values(row["sort_names"], multi_valued)
    mbids = _column_values(row["mbids"], multi_valued)
    for index, raw_name in enumerate(names):
        artist_name = raw_name.strip()
        if not artist_name or (mbid is None and artist_name != name):
            continue
        artist_mbid = clean_mbid(value_at(mbids, index))
        if artist_mbid == mbid:
            return BeetsArtist(artist_name, value_at(sort_names, index), artist_mbid)
    return None


def _column_values(value: object, multi_valued: bool) -> list[str]:
    """Return the values of a multi-valued column, or a single-valued column as a list."""
    if multi_valued:
        return split_multi_value(value)
    return [value] if isinstance(value, str) else []
