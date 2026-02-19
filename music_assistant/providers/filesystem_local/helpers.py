"""Some helpers for Filesystem based Musicproviders."""

from __future__ import annotations

import errno
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass

from music_assistant.helpers.compare import compare_strings

logger = logging.getLogger(__name__)

IGNORE_DIRS = ("recycle", "Recently-Snaphot", "#recycle", "System Volume Information", "lost+found")


@dataclass
class FileSystemItem:
    """Representation of an item (file or directory) on the filesystem.

    - filename: Name (not path) of the file (or directory).
    - relative_path: Relative path to the item on this filesystem provider.
    - absolute_path: Absolute path to this item.
    - parent_path: Absolute path to the parent directory.
    - is_dir: Boolean if item is directory (not file).
    - checksum: Checksum for this path (usually last modified time) None for dir.
    - file_size : File size in number of bytes or None if unknown (or not a file).
    - created_at: File creation timestamp (Unix epoch) or None for directories.
    """

    filename: str
    relative_path: str
    absolute_path: str
    is_dir: bool
    checksum: str | None = None
    file_size: int | None = None
    created_at: int | None = None  # file creation timestamp (Unix epoch)

    @property
    def ext(self) -> str | None:
        """Return file extension."""
        try:
            # convert to lowercase to make it case insensitive when comparing
            return self.filename.rsplit(".", 1)[1].lower()
        except IndexError:
            return None

    @property
    def name(self) -> str:
        """Return file name (without extension)."""
        return self.filename.rsplit(".", 1)[0]

    @property
    def parent_path(self) -> str:
        """Return parent path of this item."""
        return os.path.dirname(self.absolute_path)

    @property
    def parent_name(self) -> str:
        """Return parent name of this item."""
        return os.path.basename(self.parent_path)

    @property
    def relative_parent_path(self) -> str:
        """Return relative parent path of this item."""
        return os.path.dirname(self.relative_path)

    @classmethod
    def from_dir_entry(cls, entry: os.DirEntry[str], base_path: str) -> FileSystemItem:
        """Create FileSystemItem from os.DirEntry. NOT Async friendly.

        :raises OSError: If the file cannot be stat'd (e.g., invalid filename encoding).
        """
        if entry.is_dir(follow_symlinks=False):
            return cls(
                filename=entry.name,
                relative_path=get_relative_path(base_path, entry.path),
                absolute_path=entry.path,
                is_dir=True,
                checksum=None,
                file_size=None,
            )
        # This can raise OSError for files with invalid encoding (e.g., emojis on SMB mounts)
        # Let the caller handle the exception
        stat = entry.stat(follow_symlinks=False)
        # st_birthtime is available on macOS/Windows, st_ctime on Linux
        # (on Linux st_ctime is metadata change time, not creation time)
        created_at = int(getattr(stat, "st_birthtime", stat.st_ctime))
        return cls(
            filename=entry.name,
            relative_path=get_relative_path(base_path, entry.path),
            absolute_path=entry.path,
            is_dir=False,
            checksum=str(int(stat.st_mtime)),
            file_size=stat.st_size,
            created_at=created_at,
        )


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


def tokenize(input_str: str, delimiters: str) -> list[str]:
    """Tokenizes the album names or paths."""
    normalised = re.sub(delimiters, "^^^", input_str)
    return [x for x in normalised.split("^^^") if x != ""]


def _dir_contains_album_name(id3_album_name: str, directory_name: str) -> bool:
    """Check if a directory name contains an album name.

    This function tokenizes both input strings using different delimiters and
    checks if the album name is a substring of the directory name.

    First iteration considers the literal dash as one of the separators. The
    second pass is to catch edge cases where the literal dash is part of the
    album's name, not an actual separator. For example, an album like 'Aphex
    Twin - Selected Ambient Works 85-92' would be correctly handled.

    Args:
        id3_album_name (str): The album name to search for.
        directory_name (str): The directory name to search in.

    Returns:
        bool: True if the directory name contains the album name, False otherwise.
    """
    for delims in ["[-_ ]", "[_ ]"]:
        tokenized_album_name = tokenize(id3_album_name, delims)
        tokenized_dirname = tokenize(directory_name, delims)

        # Exact match, potentially just on the album name
        # in case artist's name is not included in id3_album_name
        if all(token in tokenized_dirname for token in tokenized_album_name):
            return True

        if len(tokenized_album_name) <= len(tokenized_dirname) and compare_strings(
            "".join(tokenized_album_name),
            "".join(tokenized_dirname[0 : len(tokenized_album_name)]),
            False,
        ):
            return True
    return False


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
        if compare_strings(album_name, dirname.split(" - ")[-1].split("(")[0], False):
            # account for ArtistName - AlbumName (Version) format in the directory name
            return parentdir

        if any(sep in dirname for sep in ["-", " ", "_"]) and album_name:
            album_chunks = album_name.split(" - ", 1)
            album_name_includes_artist = len(album_chunks) > 1
            just_album_name = album_chunks[1] if album_name_includes_artist else None

            # attempt matching using tokenized version of path and album name
            # with _dir_contains_album_name()
            if just_album_name and _dir_contains_album_name(just_album_name, dirname):
                return parentdir

            if _dir_contains_album_name(album_name, dirname):
                return parentdir

        if compare_strings(album_name.split("(", maxsplit=1)[0], dirname, False):
            # account for AlbumName (Version) format in the album name
            return parentdir
        if compare_strings(album_name.split("(", maxsplit=1)[0], dirname.split(" - ")[-1], False):
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


def recursive_iter(
    path: str,
    base_path: str,
    supported_extensions: set[str],
    log: logging.Logger,
) -> Iterator[FileSystemItem]:
    """Recursively traverse directory entries yielding supported files.

    :param path: The directory path to scan.
    :param base_path: The root base path for constructing relative paths.
    :param supported_extensions: Set of file extensions to include (lowercase, no dot).
    :param log: Logger instance to use for warnings/debug messages.
    """
    try:
        scan_iter = os.scandir(path)
    except OSError as err:
        if err.errno == errno.EINVAL:
            log.warning(
                "Skipping directory '%s' - unsupported characters in path",
                path,
            )
        else:
            log.warning("Unable to scan directory %s: %s", path, err)
        return
    with scan_iter:
        for item in scan_iter:
            if item.name in IGNORE_DIRS or item.name.startswith((".", "_")):
                continue
            try:
                is_dir = item.is_dir(follow_symlinks=False)
                is_file = item.is_file(follow_symlinks=False)
            except OSError as err:
                if err.errno == errno.EINVAL:
                    log.warning(
                        "Skipping '%s' - unsupported characters in name",
                        item.name,
                    )
                continue
            if is_dir:
                yield from recursive_iter(item.path, base_path, supported_extensions, log)
            elif is_file:
                if "." not in item.name:
                    continue
                ext = item.name.rsplit(".", 1)[1].lower()
                if ext not in supported_extensions:
                    continue
                try:
                    yield FileSystemItem.from_dir_entry(item, base_path)
                except OSError as err:
                    if err.errno == errno.EINVAL:
                        log.warning(
                            "Skipping '%s' - unsupported characters in name",
                            item.name,
                        )
                    else:
                        log.debug(
                            "Skipping file %s due to OS error: %s",
                            item.path,
                            str(err),
                        )


def sorted_scandir(base_path: str, sub_path: str, sort: bool = False) -> list[FileSystemItem]:
    """
    Implement os.scandir that returns (optionally) sorted entries.

    Not async friendly!
    """

    def nat_key(name: str) -> tuple[int | str, ...]:
        """Sort key for natural sorting."""
        return tuple(int(s) if s.isdigit() else s for s in re.split(r"(\d+)", name))

    if base_path not in sub_path:
        sub_path = os.path.join(base_path, sub_path)
    items: list[FileSystemItem] = []
    try:
        entries = os.scandir(sub_path)
    except OSError as err:
        if err.errno == errno.EINVAL:
            logger.warning(
                "Skipping directory '%s' - unsupported characters in path",
                sub_path,
            )
            return items
        raise
    with entries:
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as err:
                if err.errno == errno.EINVAL:
                    logger.warning(
                        "Skipping '%s' - unsupported characters in name",
                        entry.name,
                    )
                continue
            if not (is_dir or is_file):
                continue
            if entry.name in IGNORE_DIRS or entry.name.startswith("."):
                continue
            try:
                items.append(FileSystemItem.from_dir_entry(entry, base_path))
            except OSError as err:
                if err.errno == errno.EINVAL:
                    logger.warning(
                        "Skipping '%s' - unsupported characters in name",
                        entry.name,
                    )
                else:
                    logger.debug("Skipping '%s' due to OS error: %s", entry.name, err)
                continue

    if sort:
        return sorted(
            items,
            # sort by (natural) name
            key=lambda x: nat_key(x.name),
        )
    return items
