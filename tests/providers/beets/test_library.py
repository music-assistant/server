"""Tests for the read-only beets library reader."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from music_assistant.providers.beets.library import BeetsLibrary, BeetsLibraryError
from tests.providers.beets.beets_db import (
    ARTIST_MBID,
    GUEST_MBID,
    BeetsDb,
    album_fields,
    item_fields,
)


async def test_open_fails_for_missing_file_without_creating_it(tmp_path: Path) -> None:
    """A path without a database is reported as an error and never created."""
    library = BeetsLibrary(str(tmp_path / "missing.db"))
    with pytest.raises(BeetsLibraryError):
        await library.open()
    assert not (tmp_path / "missing.db").exists()


async def test_open_fails_without_items_table(tmp_path: Path) -> None:
    """A SQLite file that is not a beets library is rejected."""
    db_path = tmp_path / "other.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.commit()
    connection.close()
    library = BeetsLibrary(str(db_path))
    with pytest.raises(BeetsLibraryError, match="items"):
        await library.open()


async def test_reads_albums_and_items_with_flex_attributes(beets_db: BeetsDb) -> None:
    """Rows come back in id-ordered batches with their own flexible attributes attached."""
    album_id = beets_db.add_album(**album_fields())
    item_id = beets_db.add_item(**item_fields(album_id=album_id))
    other_id = beets_db.add_item(**item_fields(title="Other", album_id=album_id))
    beets_db.set_item_flex(item_id, "rating", "0.9")
    beets_db.set_album_flex(album_id, "mood", "calm")
    library = BeetsLibrary(str(beets_db.path))
    await library.open()
    try:
        albums = await library.get_albums()
        batches = [batch async for batch in library.iter_items(batch_size=1)]
    finally:
        await library.close()

    assert albums[album_id].flex == {"mood": "calm"}
    assert [[row.id for row in batch] for batch in batches] == [[item_id], [other_id]]
    items = {row.id: row for batch in batches for row in batch}
    assert items[item_id].flex == {"rating": "0.9"}
    assert items[other_id].flex == {}
    assert items[item_id].fields["path"] == b"Artist/Album/01 Song.flac"
    assert items[item_id].album_id == album_id


async def test_legacy_database_keeps_its_own_columns(legacy_beets_db: BeetsDb) -> None:
    """A database without the multi-value columns is read as-is."""
    item_id = legacy_beets_db.add_item(
        path=b"/home/kate/Music/old.mp3", title="Old", artist="Old Artist", genre="Jazz"
    )
    library = BeetsLibrary(str(legacy_beets_db.path))
    await library.open()
    try:
        row = await library.get_item(item_id)
    finally:
        await library.close()

    assert row is not None
    assert "genres" not in row.fields
    assert row.fields["genre"] == "Jazz"
    assert row.album_id is None


async def test_lookups_by_id_and_artist(beets_db: BeetsDb) -> None:
    """Single-row lookups return None for unknown ids and album tracks come in track order."""
    album_id = beets_db.add_album(**album_fields())
    second = beets_db.add_item(**item_fields(album_id=album_id, track=2, title="Second"))
    first = beets_db.add_item(**item_fields(album_id=album_id, track=1, title="First"))
    beets_db.add_item(
        **item_fields(title="Loose", artist="Solo", artist_sort="Solo, The", mb_artistid=GUEST_MBID)
    )
    library = BeetsLibrary(str(beets_db.path))
    await library.open()
    try:
        assert await library.count_items() == 3
        assert await library.get_item(9999) is None
        assert await library.get_album(9999) is None
        assert [row.id for row in await library.get_album_items(album_id)] == [first, second]
        assert await library.get_artist_details("Artist") == ("Artist", ARTIST_MBID)
        assert await library.get_artist_details("Solo") == ("Solo, The", GUEST_MBID)
        assert await library.get_artist_details("Nobody") is None
    finally:
        await library.close()


async def test_get_artist_details_finds_featured_artist_in_list_column(
    beets_db: BeetsDb,
) -> None:
    """A name that only appears inside the multi-valued artists list is still found."""
    beets_db.add_item(**item_fields())
    library = BeetsLibrary(str(beets_db.path))
    await library.open()
    try:
        assert await library.get_artist_details("Guest") == ("Guest", GUEST_MBID)
    finally:
        await library.close()


async def test_get_artist_details_requires_an_exact_element_match(beets_db: BeetsDb) -> None:
    """A substring of a list element, or a name using LIKE wildcard characters, is not a match."""
    beets_db.add_item(**item_fields())
    library = BeetsLibrary(str(beets_db.path))
    await library.open()
    try:
        assert await library.get_artist_details("Gue") is None
        assert await library.get_artist_details("Gu_st") is None
        assert await library.get_artist_details("%") is None
        assert await library.get_artist_details("_") is None
    finally:
        await library.close()


async def test_get_artist_details_from_list_column_skips_legacy_database(
    legacy_beets_db: BeetsDb,
) -> None:
    """A legacy database without the list columns returns None instead of raising."""
    legacy_beets_db.add_item(
        path=b"/home/kate/Music/old.mp3", title="Old", artist="Old Artist", genre="Jazz"
    )
    library = BeetsLibrary(str(legacy_beets_db.path))
    await library.open()
    try:
        assert await library.get_artist_details("Guest") is None
    finally:
        await library.close()


async def test_reading_a_closed_library_raises(beets_db: BeetsDb) -> None:
    """Reads before open() and after close() raise the library error, never a fallback value."""
    library = BeetsLibrary(str(beets_db.path))
    with pytest.raises(BeetsLibraryError):
        await library.count_items()
    with pytest.raises(BeetsLibraryError):
        await library.get_albums()
    with pytest.raises(BeetsLibraryError):
        await library.get_album(1)
    with pytest.raises(BeetsLibraryError):
        await library.get_artist_details("Artist")

    await library.open()
    await library.close()
    with pytest.raises(BeetsLibraryError):
        await library.count_items()
    with pytest.raises(BeetsLibraryError):
        await library.get_albums()
    with pytest.raises(BeetsLibraryError):
        await library.get_album(1)
    with pytest.raises(BeetsLibraryError):
        await library.get_artist_details("Artist")


async def test_locked_database_raises_library_error(beets_db: BeetsDb) -> None:
    """A read while beets holds a write lock surfaces as a library error."""
    beets_db.add_item(**item_fields())
    with patch("music_assistant.providers.beets.library.SQLITE_BUSY_TIMEOUT", 0.1):
        library = BeetsLibrary(str(beets_db.path))
        await library.open()
    locker = sqlite3.connect(beets_db.path)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(BeetsLibraryError, match="locked"):
            await library.count_items()
    finally:
        locker.rollback()
        locker.close()
        await library.close()


async def test_never_writes_to_the_database(beets_db: BeetsDb) -> None:
    """Reading leaves the file byte-identical and the connection refuses writes."""
    album_id = beets_db.add_album(**album_fields())
    item_id = beets_db.add_item(**item_fields(album_id=album_id))
    beets_db.set_item_flex(item_id, "rating", "0.9")
    before = beets_db.path.read_bytes()
    library = BeetsLibrary(str(beets_db.path))
    await library.open()
    try:
        await library.get_albums()
        _ = [batch async for batch in library.iter_items()]
        await library.get_item(item_id)
        assert library._db is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await library._db.execute("DELETE FROM items")
    finally:
        await library.close()
    assert beets_db.path.read_bytes() == before
