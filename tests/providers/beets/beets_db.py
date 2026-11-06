"""Build beets library databases for the beets provider tests, with beets itself."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import beets
from beets.dbcore.types import MULTI_VALUE_DELIMITER
from beets.library import Album, Item, Library

ARTIST_MBID = "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"
GUEST_MBID = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
ALBUM_MBID = "1dc4c347-a1db-32aa-b14f-bc9cc507b843"
RELEASE_GROUP_MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
TRACK_MBID = "b1a9c0e9-d987-4042-ae91-78d6a3267d69"

__all__ = ["MULTI_VALUE_DELIMITER"]


def album_fields(**overrides: Any) -> dict[str, Any]:
    """
    Return the fields of a typical beets album.

    :param overrides: Fields to replace or add.
    """
    return {
        "album": "Album",
        "albumartist": "Artist",
        "albumartist_sort": "Artist",
        "mb_albumartistid": ARTIST_MBID,
        "albumartists": ["Artist"],
        "albumartists_sort": ["Artist"],
        "mb_albumartistids": [ARTIST_MBID],
        "albumtype": "album",
        "albumtypes": ["album"],
        "comp": False,
        "year": 2001,
        "original_year": 1999,
        "mb_albumid": ALBUM_MBID,
        "mb_releasegroupid": RELEASE_GROUP_MBID,
        "barcode": "0123456789012",
        "asin": "B000002UAL",
        "label": "Label",
        "genres": ["Rock", "Indie"],
        **overrides,
    }


def item_fields(**overrides: Any) -> dict[str, Any]:
    """
    Return the fields of a typical beets item.

    :param overrides: Fields to replace or add.
    """
    return {
        "path": b"Artist/Album/01 Song.flac",
        "title": "Song",
        "artist": "Artist feat. Guest",
        "artist_sort": "Artist feat. Guest",
        "mb_artistid": ARTIST_MBID,
        "artists": ["Artist", "Guest"],
        "artists_sort": ["Artist", "Guest"],
        "mb_artistids": [ARTIST_MBID, GUEST_MBID],
        "albumartist": "Artist",
        "album": "Album",
        "genres": ["Rock", "Indie"],
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
    """A beets library database on disk, written by beets, that tests can fill and edit."""

    def __init__(self, path: Path, music_directory: Path) -> None:
        """
        Create the database with beets, so it holds every table and migration of beets.

        :param path: Where to create the database.
        :param music_directory: beets' music directory, which relative item paths sit under.
        """
        self.path = path
        self.music_directory = music_directory
        # the tests never read the user's beets configuration, nor back up on migration
        beets.config.clear()
        beets.config.read(user=False, defaults=True)
        beets.config["create_backup_before_migrations"] = False
        self._library = Library(str(path), directory=str(music_directory), set_music_dir=False)

    def close(self) -> None:
        """Close the beets connections of the test database."""
        self._library._close()

    def add_album(self, **fields: Any) -> int:
        """Add an album without items and return its id."""
        with self._library.music_dir_context():
            album = Album(self._library, **fields)
            album.add(self._library)
        return int(album.id or 0)

    def add_item(self, **fields: Any) -> int:
        """Add an item and return its id."""
        with self._library.music_dir_context():
            return int(self._library.add(Item(**fields)) or 0)

    def update_album(self, album_id: int, **fields: Any) -> None:
        """Change fields of an album, without writing them to its items."""
        with self._library.music_dir_context():
            album = self._album(album_id)
            album.update(fields)
            album.store(inherit=False)

    def update_item(self, item_id: int, **fields: Any) -> None:
        """Change fields of an item."""
        with self._library.music_dir_context():
            item = self._item(item_id)
            item.update(fields)
            item.store()

    def delete_item(self, item_id: int) -> None:
        """Delete an item, keeping its album even when it was the last item."""
        with self._library.music_dir_context():
            self._item(item_id).remove(with_album=False)

    def delete_album(self, album_id: int) -> None:
        """Delete an album together with its items, the way `beet remove -a` does."""
        with self._library.music_dir_context():
            self._album(album_id).remove(with_items=True)

    def set_item_flex(self, item_id: int, key: str, value: Any) -> None:
        """Set a flexible attribute on an item."""
        self.update_item(item_id, **{key: value})

    def set_album_flex(self, album_id: int, key: str, value: Any) -> None:
        """Set a flexible attribute on an album."""
        self.update_album(album_id, **{key: value})

    def _item(self, item_id: int) -> Item:
        item = self._library.get_item(item_id)
        assert item is not None
        return item

    def _album(self, album_id: int) -> Album:
        album = self._library.get_album(album_id)
        assert album is not None
        return album
