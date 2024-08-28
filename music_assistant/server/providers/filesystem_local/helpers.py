"""Some helpers for Filesystem based Musicproviders."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from music_assistant.server.helpers.compare import compare_strings

IGNORE_DIRS = ("recycle", "Recently-Snaphot")


@dataclass
class FileSystemItem:
    """Representation of an item (file or directory) on the filesystem.

    - filename: Name (not path) of the file (or directory).
    - path: Relative path to the item on this filesystem provider.
    - absolute_path: Absolute path to this item.
    - is_file: Boolean if item is file (not directory or symlink).
    - is_dir: Boolean if item is directory (not file).
    - checksum: Checksum for this path (usually last modified time).
    - file_size : File size in number of bytes or None if unknown (or not a file).
    """

    filename: str
    path: str
    absolute_path: str
    is_file: bool
    is_dir: bool
    checksum: str
    file_size: int | None = None

    @property
    def ext(self) -> str | None:
        """Return file extension."""
        try:
            return self.filename.rsplit(".", 1)[1]
        except IndexError:
            return None

    @property
    def name(self) -> str:
        """Return file name (without extension)."""
        return self.filename.rsplit(".", 1)[0]


def get_artist_dir(
    artist_name: str,
    album_dir: str | None,
) -> str | None:
    """Look for (Album)Artist directory in path of a track (or album)."""
    if not album_dir:
        return None
    parentdir = os.path.dirname(album_dir)
    # account for disc or album sublevel by ignoring (max) 2 levels if needed
    matched_dir: str | None = None
    for _ in range(3):
        dirname = parentdir.rsplit(os.sep)[-1]
        if compare_strings(artist_name, dirname, False):
            # literal match
            # we keep hunting further down to account for the
            # edge case where the album name has the same name as the artist
            matched_dir = parentdir
        parentdir = os.path.dirname(parentdir)
    return matched_dir


def get_album_dir(track_dir: str, album_name: str) -> str | None:
    """Return album/parent directory of a track."""
    parentdir = track_dir
    # account for disc sublevel by ignoring 1 level if needed
    for _ in range(2):
        dirname = parentdir.rsplit(os.sep)[-1]
        if compare_strings(album_name, dirname, False):
            # literal match
            return parentdir
        if compare_strings(album_name, dirname.split(" - ")[-1], False):
            # account for ArtistName - AlbumName format in the directory name
            return parentdir
        if compare_strings(album_name, dirname.split("(")[0], False):
            # account for ArtistName - AlbumName (Version) format in the directory name
            return parentdir
        if compare_strings(album_name.split("(")[0], dirname, False):
            # account for AlbumName (Version) format in the album name
            return parentdir
        if compare_strings(album_name.split("(")[0], dirname.split(" - ")[-1], False):
            # account for ArtistName - AlbumName (Version) format
            return parentdir
        if len(album_name) > 8 and album_name in dirname:
            # dirname contains album name
            # (could potentially lead to false positives, hence the length check)
            return parentdir
        parentdir = os.path.dirname(parentdir)
    return None


def get_relative_path(base_path: str, path: str) -> str:
    """Return the relative path string for a path."""
    if path.startswith(base_path):
        path = path.split(base_path)[1]
    for sep in ("/", "\\"):
        if path.startswith(sep):
            path = path[1:]
    return path


def get_absolute_path(base_path: str, path: str) -> str:
    """Return the absolute path string for a path."""
    if path.startswith(base_path):
        return path
    return os.path.join(base_path, path)


def sorted_scandir(base_path: str, sub_path: str, sort: bool = False) -> list[FileSystemItem]:
    """
    Implement os.scandir that returns (optionally) sorted entries.

    Not async friendly!
    """

    def nat_key(name: str) -> tuple[int | str, ...]:
        """Sort key for natural sorting."""
        return tuple(int(s) if s.isdigit() else s for s in re.split(r"(\d+)", name))

    def create_item(entry: os.DirEntry) -> FileSystemItem:
        """Create FileSystemItem from os.DirEntry."""
        absolute_path = get_absolute_path(base_path, entry.path)
        stat = entry.stat(follow_symlinks=False)
        return FileSystemItem(
            filename=entry.name,
            path=get_relative_path(base_path, entry.path),
            absolute_path=absolute_path,
            is_file=entry.is_file(follow_symlinks=False),
            is_dir=entry.is_dir(follow_symlinks=False),
            checksum=str(int(stat.st_mtime)),
            file_size=stat.st_size,
        )

    items = [
        create_item(x)
        for x in os.scandir(sub_path)
        # filter out invalid dirs and hidden files
        if x.name not in IGNORE_DIRS and not x.name.startswith(".")
    ]
    if sort:
        return sorted(
            items,
            # sort by (natural) name
            key=lambda x: nat_key(x.name),
        )
    return items
