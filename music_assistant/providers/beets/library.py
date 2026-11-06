"""Read-only access to a beets library through beets' own library API."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import beets
from beets.dbcore import MatchQuery, Query
from beets.dbcore.db import DBAccessError
from beets.dbcore.query import NumericQuery, RegexpQuery
from beets.dbcore.sort import FixedFieldSort, MultipleSort
from beets.dbcore.types import MULTI_VALUE_DELIMITER, MULTI_VALUE_DSV
from beets.library import Album, Item, Library
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.helpers.tags import clean_mbid

from .constants import ITEM_BATCH_SIZE, SQLITE_BUSY_TIMEOUT

# album artists are looked up before track artists and, like the parsers read them, the
# multi-valued lists before the single-valued fields: (model, list fields, single fields)
_ARTIST_LOOKUPS: tuple[
    tuple[type[Album | Item], tuple[str, str, str], tuple[str, str, str]], ...
] = (
    (
        Album,
        ("albumartists", "albumartists_sort", "mb_albumartistids"),
        ("albumartist", "albumartist_sort", "mb_albumartistid"),
    ),
    (
        Item,
        ("artists", "artists_sort", "mb_artistids"),
        ("artist", "artist_sort", "mb_artistid"),
    ),
)
_DELIMITER_PATTERN = re.escape(MULTI_VALUE_DELIMITER)


def split_multi_value(value: object) -> list[str]:
    """
    Return the values of a beets multi-valued field, keeping empty positions.

    :param value: The field value, as a list or as beets' delimited text.
    """
    if isinstance(value, list):
        return [str(element) for element in value]
    if isinstance(value, str) and value:
        return MULTI_VALUE_DSV.parse(value)
    return []


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


class BeetsUpgradeRequiredError(ProviderUnavailableError):
    """Raised when the database still needs schema changes or migrations of the pinned beets."""


@dataclass
class BeetsRow:
    """A beets item or album with its fixed fields and its flexible attributes."""

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


class _ReadOnlyLibrary(Library):
    """A beets library whose database connections never write."""

    def _create_connection(self) -> sqlite3.Connection:
        """Return a beets database connection that refuses every write."""
        conn = super()._create_connection()
        # opening runs beets' schema setup and data migrations; a database that still needs
        # any of them fails to open instead of being changed
        conn.execute("PRAGMA query_only = ON")
        return conn


class BeetsLibrary:
    """Read-only reader for a beets library."""

    def __init__(self, db_path: str, music_directory: str) -> None:
        """
        Initialize the reader.

        :param db_path: Path to the beets library database.
        :param music_directory: Where beets' music directory is mounted for Music Assistant;
            the paths beets stores relative to its music directory resolve against it.
        """
        self.db_path = db_path
        self.music_directory = music_directory
        self._library: Library | None = None

    async def open(self) -> None:
        """
        Open the beets library read-only.

        :raises BeetsUpgradeRequiredError: If the database still needs schema changes or
            migrations of the beets version Music Assistant uses.
        :raises ProviderUnavailableError: If the database cannot be opened or read.
        """
        await self.close()
        self._library = await asyncio.to_thread(self._open_library)

    async def close(self) -> None:
        """Close the database connections if the library is open."""
        if (library := self._library) is not None:
            self._library = None
            # beets has no public close; this closes the connections of every thread
            await asyncio.to_thread(library._close)

    async def max_item_id(self) -> int:
        """Return the highest beets item id, or 0 for an empty library."""
        newest = await self._run(
            lambda library: [
                _row(item)
                for item in library.items(sort=FixedFieldSort("id", ascending=False), limit=1)
            ]
        )
        return newest[0].id if newest else 0

    async def get_albums(self) -> dict[int, BeetsRow]:
        """Return every album keyed by its beets id."""
        return await self._run(lambda library: {row.id: row for row in map(_row, library.albums())})

    async def iter_items(self, batch_size: int = ITEM_BATCH_SIZE) -> AsyncGenerator[list[BeetsRow]]:
        """
        Yield all items in batches ordered by id.

        No lock is held on the database between batches, so beets can keep writing to it.

        :param batch_size: Maximum number of items per batch.
        """
        last_id = 0
        while True:

            def _batch(library: Library, after: int = last_id) -> list[BeetsRow]:
                return [
                    _row(item)
                    for item in library.items(
                        NumericQuery("id", f"{after + 1}.."),
                        sort=FixedFieldSort("id"),
                        limit=batch_size,
                    )
                ]

            batch = await self._run(_batch)
            if not batch:
                return
            yield batch
            last_id = batch[-1].id

    async def get_item(self, item_id: int) -> BeetsRow | None:
        """
        Return one item, or None when beets has no item with this id.

        :param item_id: The beets item id.
        """
        return await self._run(
            lambda library: _row(item) if (item := library.get_item(item_id)) else None
        )

    async def get_album(self, album_id: int) -> BeetsRow | None:
        """
        Return one album, or None when beets has no album with this id.

        :param album_id: The beets album id.
        """
        return await self._run(
            lambda library: _row(album) if (album := library.get_album(album_id)) else None
        )

    async def get_album_items(self, album_id: int) -> list[BeetsRow]:
        """
        Return the items of an album in disc and track order.

        :param album_id: The beets album id.
        """
        order = MultipleSort([FixedFieldSort(name) for name in ("disc", "track", "id")])
        return await self._run(
            lambda library: [
                _row(item) for item in library.items(MatchQuery("album_id", album_id), sort=order)
            ]
        )

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
        return await self._run(lambda library: _find_artist(library, name, mbid))

    def _open_library(self) -> Library:
        """Open the beets library; runs in a worker thread."""
        # beets would otherwise create its library file when the path holds none
        if not Path(self.db_path).is_file():
            msg = f"{self.db_path} does not exist"
            raise ProviderUnavailableError(msg)
        _configure_beets()
        try:
            return _ReadOnlyLibrary(
                self.db_path, directory=self.music_directory, set_music_dir=False
            )
        except (DBAccessError, sqlite3.Error) as err:
            if _is_write_refusal(err):
                msg = (
                    f"{self.db_path} needs schema changes or migrations of beets "
                    f"{beets.__version__}; run any beet command with beets {beets.__version__} "
                    "or newer once"
                )
                raise BeetsUpgradeRequiredError(msg) from err
            msg = f"Unable to open {self.db_path}: {err}"
            raise ProviderUnavailableError(msg) from err

    async def _run[T](self, read: Callable[[Library], T]) -> T:
        """
        Run a read against the open library in a worker thread.

        :param read: The read, given the beets library.
        :raises ProviderUnavailableError: If the library is not open or cannot be read.
        """
        if (library := self._library) is None:
            msg = f"The beets library {self.db_path} is not open"
            raise ProviderUnavailableError(msg)

        def _read() -> T:
            # beets resolves its stored relative paths against the music directory bound here
            with library.music_dir_context():
                return read(library)

        try:
            return await asyncio.to_thread(_read)
        except (DBAccessError, sqlite3.Error) as err:
            msg = f"Unable to read {self.db_path}: {err}"
            raise ProviderUnavailableError(msg) from err


def _configure_beets() -> None:
    """Point beets' global configuration at its defaults only."""
    # reading the user configuration would also create a beets config directory for the user
    # Music Assistant runs as; the backup beets makes before a migration would be a new file
    # next to the database
    beets.config.clear()
    beets.config.read(user=False, defaults=True)
    beets.config["create_backup_before_migrations"] = False
    beets.config["timeout"] = SQLITE_BUSY_TIMEOUT


def _is_write_refusal(err: BaseException) -> bool:
    """Return whether an error, or an error it was raised from, is a refused write."""
    current: BaseException | None = err
    while current is not None:
        if (
            isinstance(current, sqlite3.Error)
            and (current.sqlite_errorcode & 0xFF) == sqlite3.SQLITE_READONLY
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _row(model: Album | Item) -> BeetsRow:
    """Return the row of a beets album or item, read without an item's album fields."""
    if isinstance(model, Item):
        values = {key: model.get(key, with_album=False) for key in model.keys(with_album=False)}
    else:
        values = {key: model.get(key) for key in model}
    fixed = type(model)._fields
    return BeetsRow(
        id=model.id or 0,
        fields={key: value for key, value in values.items() if key in fixed},
        flex={key: value for key, value in values.items() if key not in fixed},
    )


def _find_artist(library: Library, name: str | None, mbid: str | None) -> BeetsArtist | None:
    """Return the first matching artist in the album artists, then the track artists."""
    for model, list_fields, single_fields in _ARTIST_LOOKUPS:
        for fields, multi_valued in ((list_fields, True), (single_fields, False)):
            query = _artist_query(fields, multi_valued, name, mbid)
            sort = FixedFieldSort("id")
            candidates = (
                library.albums(query, sort) if model is Album else library.items(query, sort)
            )
            for candidate in candidates:
                if artist := _matching_artist(candidate, fields, multi_valued, name, mbid):
                    return artist
    return None


def _artist_query(
    fields: tuple[str, str, str], multi_valued: bool, name: str | None, mbid: str | None
) -> Query:
    """Return a query for the rows that may hold the artist, checked exactly afterwards."""
    name_field, _sort_field, mbid_field = fields
    if mbid:
        # stored ids may be in another case or padded, so the match is made on the clean id
        return RegexpQuery(mbid_field, f"(?i){re.escape(mbid)}")
    escaped = re.escape(name or "")
    if multi_valued:
        return RegexpQuery(
            name_field, rf"(?:^|{_DELIMITER_PATTERN})\s*{escaped}\s*(?:{_DELIMITER_PATTERN}|$)"
        )
    return RegexpQuery(name_field, rf"^\s*{escaped}\s*$")


def _matching_artist(
    model: Album | Item,
    fields: tuple[str, str, str],
    multi_valued: bool,
    name: str | None,
    mbid: str | None,
) -> BeetsArtist | None:
    """Return the artist of a row with this MusicBrainz id, or with this name and no id."""
    names, sort_names, mbids = (_field_values(model, key, multi_valued) for key in fields)
    for index, raw_name in enumerate(names):
        artist_name = raw_name.strip()
        if not artist_name or (mbid is None and artist_name != name):
            continue
        artist_mbid = clean_mbid(value_at(mbids, index))
        if artist_mbid == mbid:
            return BeetsArtist(artist_name, value_at(sort_names, index), artist_mbid)
    return None


def _field_values(model: Album | Item, key: str, multi_valued: bool) -> list[str]:
    """Return the values of a multi-valued field, or a single-valued field as a list."""
    value = model.get(key, with_album=False) if isinstance(model, Item) else model.get(key)
    if multi_valued:
        return split_multi_value(value)
    return [value] if isinstance(value, str) else []
