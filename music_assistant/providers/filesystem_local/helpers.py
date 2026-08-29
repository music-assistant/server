"""Some helpers for Filesystem based Musicproviders."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

import xmltodict
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.json import make_utf8_safe
from music_assistant.helpers.security import is_safe_path

from .constants import IMAGE_EXTENSIONS, METADATA_IMAGE_STEMS, NFO_FILENAMES

logger = logging.getLogger(__name__)

# number of consecutive unreadable directories that marks the storage itself as gone
MAX_CONSECUTIVE_SCAN_ERRORS = 10

# number of example paths kept for the scan summary the user gets to see
MAX_REPORTED_FAILED_PATHS = 5

IGNORE_DIRS = (
    "recycle",
    "Recently-Snaphot",
    "Recently-Snapshot",
    "#recycle",
    "System Volume Information",
    "lost+found",
    "@eaDir",
)


@dataclass
class ScanErrors:
    """
    Error state of a single (recursive) scan of a filesystem provider.

    Shared by all directory levels of one scan, so a storage that goes away
    halfway through is detected after a handful of failures instead of failing
    once per remaining directory.

    - fatal: The error that ended the scan: the provider root itself is unreadable
      or too many directories failed in a row. Callers abort the sync and mark
      the provider unavailable.
    - failed_dirs: Number of directories that could not be read. A scan with
      failed directories is incomplete, so callers must not run deletions.
    - failed_entries: Number of files that could not be read or processed. Those
      files are missing from the scan result too, so they block deletions as well.
    - failed_paths: The first few paths that could not be read, named in the summary
      so the user can find them without turning on debug logging.
    - consecutive_failures: Directories that failed since the last one read
      successfully, excluding failures that do not point at unreachable storage.
    """

    fatal: Exception | None = None
    failed_dirs: int = 0
    failed_entries: int = 0
    consecutive_failures: int = 0
    failed_paths: list[str] = field(default_factory=list)

    @property
    def aborted(self) -> bool:
        """Return True if the scan must be stopped."""
        return self.fatal is not None

    @property
    def incomplete(self) -> bool:
        """Return True if the scan missed content that is still on the storage."""
        return bool(self.failed_dirs or self.failed_entries)

    def describe(self) -> str:
        """Return a summary of what this scan could not read. Only meaningful when incomplete."""
        parts = []
        if self.failed_dirs:
            parts.append(f"{self.failed_dirs} folder(s)")
        if self.failed_entries:
            parts.append(f"{self.failed_entries} file(s)")
        summary = f"{' and '.join(parts)} could not be read"
        if self.failed_paths:
            summary += f" (e.g. {', '.join(self.failed_paths)})"
        return summary

    def record_dir_read(self) -> None:
        """Register a directory that was read successfully."""
        self.consecutive_failures = 0

    def record_dir_error(
        self,
        err: Exception,
        *,
        is_root: bool,
        counts_toward_abort: bool = True,
        path: str | None = None,
    ) -> None:
        """
        Register a directory that could not be read.

        :param err: The error raised while reading the directory.
        :param is_root: True if the directory is the provider's root path.
        :param counts_toward_abort: False for an error that leaves the scan incomplete
            but says nothing about the storage being reachable, such as a folder that
            is only permission-denied.
        :param path: Path of the directory, named in the summary shown to the user.
        """
        if is_root:
            self.fatal = err
            return
        self.failed_dirs += 1
        self._remember_path(path)
        if not counts_toward_abort:
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_SCAN_ERRORS:
            self.fatal = err

    def record_entry_error(self, err: Exception, path: str | None = None) -> None:
        """
        Register a file that could not be read or processed.

        :param err: The error raised while reading or processing the file.
        :param path: Path of the file, named in the summary shown to the user.
        """
        # a file that disappeared between the listing and the read is a normal race
        # during a long scan, and it really is gone, so deletions may handle it
        if getattr(err, "errno", None) == errno.ENOENT:
            return
        self.failed_entries += 1
        self._remember_path(path)

    def _remember_path(self, path: str | None) -> None:
        """Keep the first few failed paths as examples for the user."""
        if path and len(self.failed_paths) < MAX_REPORTED_FAILED_PATHS:
            self.failed_paths.append(path)


@dataclass
class FileSystemItem:
    """
    Representation of an item (file or directory) on the filesystem.

    - filename: Name (not path) of the file (or directory).
    - relative_path: Relative path to the item on this filesystem provider.
    - absolute_path: Absolute path to this item.
    - is_dir: Boolean if item is directory (not file).
    - checksum: Checksum for this path (usually last modified time) None for dir.
    - file_size : File size in number of bytes or None if unknown (or not a file).
    - created_at: File creation timestamp (Unix epoch) or None for directories.
    - metadata_token: A higher-precision change token (e.g. a local nanosecond mtime or a
      WebDAV ETag), used only to detect a local metadata file (NFO/image) changing; the
      imported-media ``checksum`` is unaffected and stays whatever it always was.
    """

    filename: str
    relative_path: str
    absolute_path: str
    is_dir: bool
    checksum: str | None = None
    file_size: int | None = None
    created_at: int | None = None  # file creation timestamp (Unix epoch)
    metadata_token: str | None = None

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
    def parent_name(self) -> str:
        """Return the name of this item's parent directory."""
        # derived from the relative path: the absolute path may be a URL on
        # network/cloud providers (webdav, cloud filesystems)
        return Path(self.relative_parent_path).name

    @property
    def relative_parent_path(self) -> str:
        """Return relative parent path of this item."""
        return os.path.dirname(self.relative_path)

    @property
    def metadata_change_token(self) -> str | None:
        """Return the highest-precision token available for local metadata-file tracking."""
        return self.metadata_token or self.checksum

    @classmethod
    def from_dir_entry(cls, entry: os.DirEntry[str], base_path: str) -> FileSystemItem:
        """
        Create FileSystemItem from os.DirEntry. NOT Async friendly.

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
            metadata_token=str(stat.st_mtime_ns),
        )


def is_metadata_file(item: FileSystemItem) -> bool:
    """
    Return True for a recognized local metadata file (album/artist NFO or a folder image).

    Metadata files are never imported as media: they carry no provider mapping of their own
    and are only used to detect a change worth reparsing their representative track.

    :param item: The file to check.
    """
    if item.is_dir or not item.ext:
        return False
    ext = item.ext.lower()
    if ext == "nfo":
        return item.filename.lower() in NFO_FILENAMES
    if ext in IMAGE_EXTENSIONS:
        return item.name.lower() in METADATA_IMAGE_STEMS
    return False


def is_image_file(item: FileSystemItem) -> bool:
    """
    Return True when a recognized metadata file is a folder image rather than an NFO file.

    :param item: The file to check; only meaningful for a file that :func:`is_metadata_file`
        already accepted.
    """
    return item.ext is not None and item.ext.lower() in IMAGE_EXTENSIONS


# folder names that denote a disc/volume subfolder underneath an album folder; its own NFO is
# never trusted as the album's identity, only the parent's is
_DISC_DIR_RE = re.compile(r"^(?:disc|disk|cd|dvd|vol(?:ume)?)[\s._-]*\d+\b", re.IGNORECASE)


def is_disc_dir(name: str) -> bool:
    """Return True when a folder name looks like a disc subfolder (e.g. ``Disc 1``, ``CD2``)."""
    return bool(_DISC_DIR_RE.match(name.strip()))


def parse_nfo_root(data: bytes, root_tag: str) -> dict[str, Any] | None:
    """
    Parse an NFO file's bytes and return its expected root element, or None when malformed.

    :param data: The raw NFO file content.
    :param root_tag: The expected root element name (``album`` or ``artist``).
    """
    try:
        text = data.decode("utf-8")
        parsed = xmltodict.parse(text)
    except UnicodeDecodeError, ExpatError, ValueError:
        return None
    root = parsed.get(root_tag)
    return root if isinstance(root, dict) else None


def get_folder_signature(items: list[FileSystemItem]) -> str:
    """
    Return an order-independent digest of the given files' paths, mtimes and sizes.

    Intended as a cache checksum: any file added, removed, replaced or retagged changes it.

    :param items: The files to include in the digest.
    """
    parts = sorted(f"{x.relative_path}\0{x.checksum}\0{x.file_size}" for x in items)
    return hashlib.sha256("\0\0".join(parts).encode()).hexdigest()


def get_artist_dir(
    artist_name: str,
    album_dir: str | None,
    sort_name: str | None = None,
    *,
    exact_only: bool = False,
) -> str | None:
    """
    Look for (Album)Artist directory in path of a track (or album).

    :param artist_name: The artist name to match against a folder name.
    :param album_dir: The album directory whose ancestors are searched.
    :param sort_name: The artist sort name, tried as an alias when the plain name does not
        match a folder (e.g. a folder named ``Beatles, The`` for the artist ``The Beatles``).
        Ignored when `exact_only` is set: a sort-name alias is itself a relaxed heuristic.
    :param exact_only: Only accept an exact (normalized) match of the plain name, skipping
        the relaxed (fuzzy) fallback built into the default comparison.
    """
    if not album_dir:
        return None
    # the plain name's own bounded search completes in full before the sort-name alias is
    # ever tried, so a farther (grandparent-level) alias match can never outrank a nearer,
    # exact plain-name match
    candidate_names = (artist_name,) if exact_only else (n for n in (artist_name, sort_name) if n)
    for candidate_name in candidate_names:
        parentdir = os.path.dirname(album_dir)
        matched_dir: str | None = None
        # account for disc or album sublevel by ignoring (max) 2 levels if needed
        for _ in range(3):
            dirname = Path(parentdir).name
            if compare_strings(candidate_name, dirname, exact_only):
                # literal match
                # we keep hunting further down to account for the
                # edge case where the album name has the same name as the artist
                matched_dir = parentdir
            parentdir = os.path.dirname(parentdir)
        if matched_dir:
            return matched_dir
    return None


def tokenize(input_str: str, delimiters: str) -> list[str]:
    """Tokenizes the album names or paths."""
    normalised = re.sub(delimiters, "^^^", input_str)
    return [x for x in normalised.split("^^^") if x != ""]


def _dir_contains_album_name(id3_album_name: str, directory_name: str) -> bool:
    """
    Check if a directory name contains an album name.

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


# a recognized leading release-date/year marker: YYYY-MM-DD, YYYY.MM.DD, a bare/parenthesized/
# bracketed YYYY - each only when followed by a real separator, so an arbitrary leading number
# (e.g. a catalogue prefix like "CAT-1234", which does not even start with 4 digits) is untouched
_DATE_PREFIX_RE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}|\d{4}\.\d{2}\.\d{2}|\(\d{4}\)|\[\d{4}\]|\d{4})(?=[\s._-])[\s._-]+"
)


def _strip_date_prefix(name: str) -> str:
    """Strip one recognized leading release-date/year marker from a folder name, if present."""
    return _DATE_PREFIX_RE.sub("", name, count=1)


def _dir_matches_album(dirname: str, album_name: str) -> bool:
    """
    Return True when a directory name matches an album name, allowing common layout variants.

    :param dirname: The directory name to test.
    :param album_name: The album name (or an alias such as the album sort name) to match.
    """
    if compare_strings(album_name, dirname, False):
        # literal match
        return True
    if (stripped := _strip_date_prefix(dirname)) != dirname and compare_strings(
        album_name, stripped, True
    ):
        # a leading release date/year (e.g. "2025-03-14 Album Name") is common release
        # folder naming but not part of the album's own title; comparing what remains with
        # strict normalized equality (not token/fuzzy matching) keeps word order significant
        return True
    if compare_strings(album_name, dirname.rsplit(" - ", maxsplit=1)[-1], False):
        # account for ArtistName - AlbumName format in the directory name
        return True
    if compare_strings(
        album_name, dirname.rsplit(" - ", maxsplit=1)[-1].split("(", maxsplit=1)[0], False
    ):
        # account for ArtistName - AlbumName (Version) format in the directory name
        return True
    if any(sep in dirname for sep in ["-", " ", "_"]):
        album_chunks = album_name.split(" - ", 1)
        just_album_name = album_chunks[1] if len(album_chunks) > 1 else None
        # attempt matching using tokenized version of path and album name
        # with _dir_contains_album_name()
        if just_album_name and _dir_contains_album_name(just_album_name, dirname):
            return True
        if _dir_contains_album_name(album_name, dirname):
            return True
    if compare_strings(album_name.split("(", maxsplit=1)[0], dirname, False):
        # account for AlbumName (Version) format in the album name
        return True
    if compare_strings(
        album_name.split("(", maxsplit=1)[0], dirname.rsplit(" - ", maxsplit=1)[-1], False
    ):
        # account for ArtistName - AlbumName (Version) format
        return True
    # dirname contains album name (could potentially lead to false positives, hence the length check)
    return len(album_name) > 8 and album_name in dirname


def get_album_dir(
    track_dir: str,
    album_name: str,
    album_sort: str | None = None,
    *,
    exact_only: bool = False,
) -> str | None:
    """
    Return the album (or parent) directory of a track, or None when no folder matches.

    :param track_dir: The directory the track file lives in.
    :param album_name: The album name to match against a folder name.
    :param album_sort: The album sort name, tried as an alias when the plain name does not
        match a folder (e.g. a folder named ``Wall, The`` for the album ``The Wall``).
        Ignored when `exact_only` is set: a sort-name alias is itself a relaxed heuristic.
    :param exact_only: Only accept an exact (normalized) match of the plain name, skipping
        every relaxed layout/alias/date-prefix heuristic below.
    """
    # the plain name's own bounded search (nearer level first) completes in full before the
    # sort-name alias is ever tried, so an alias match at track_dir can never outrank an exact
    # plain-name match at its parent
    candidate_names = (
        (album_name,) if exact_only else (name for name in (album_name, album_sort) if name)
    )
    for candidate_name in candidate_names:
        parentdir = track_dir
        # account for disc sublevel by ignoring 1 level if needed
        for _ in range(2):
            dirname = Path(parentdir).name
            matches = (
                compare_strings(candidate_name, dirname, True)
                if exact_only
                else _dir_matches_album(dirname, candidate_name)
            )
            if matches:
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
    """
    Return the absolute path for a path, constrained to base_path.

    :raises MediaNotFoundError: If the resolved path escapes base_path
        (e.g. via ``../`` traversal or an absolute path outside the base).
    """
    absolute_path = path if path.startswith(base_path) else os.path.join(base_path, path)
    if not is_safe_path(absolute_path, base_path):
        msg = f"Path is outside the configured base directory: {path}"
        raise MediaNotFoundError(msg)
    return absolute_path


def recursive_iter(
    path: str,
    base_path: str,
    supported_extensions: set[str],
    log: logging.Logger,
    scan_errors: ScanErrors | None = None,
) -> Iterator[FileSystemItem]:
    """
    Recursively traverse directory entries yielding supported files.

    :param path: The directory path to scan.
    :param base_path: The root base path for constructing relative paths.
    :param supported_extensions: Set of file extensions to include (lowercase, no dot).
    :param log: Logger instance to use for warnings/debug messages.
    :param scan_errors: Optional state object collecting the errors raised during this
        scan. Callers treat ``fatal`` as "provider unreachable" and abort the sync.
    """
    if scan_errors is None:
        scan_errors = ScanErrors()
    try:
        scan_iter = os.scandir(path)
    except OSError as err:
        if err.errno == errno.EINVAL:
            log.warning(
                "Skipping directory '%s' - unsupported characters in path",
                path,
            )
            return
        log.warning("Unable to scan directory %s: %s", path, err)
        _record_dir_failure(scan_errors, err, path=path, base_path=base_path, log=log)
        return
    entry_error_logged = False
    with scan_iter:
        while True:
            try:
                item = next(scan_iter)
            except StopIteration:
                scan_errors.record_dir_read()
                break
            except OSError as err:
                log.warning("Error while scanning directory %s: %s", path, err)
                _record_dir_failure(scan_errors, err, path=path, base_path=base_path, log=log)
                return
            if (
                item.name in IGNORE_DIRS
                or item.name.startswith((".", "_"))
                or _skip_undecodable_name(item.name, log)
            ):
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
                else:
                    # the entry may well be a directory, so this can hide a whole subtree
                    entry_error_logged = _record_entry_failure(
                        scan_errors,
                        err,
                        entry_path=item.path,
                        base_path=base_path,
                        log=log,
                        already_logged=entry_error_logged,
                    )
                continue
            if is_dir:
                yield from recursive_iter(
                    item.path,
                    base_path,
                    supported_extensions,
                    log,
                    scan_errors,
                )
                if scan_errors.aborted:
                    return
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
                        entry_error_logged = _record_entry_failure(
                            scan_errors,
                            err,
                            entry_path=item.path,
                            base_path=base_path,
                            log=log,
                            already_logged=entry_error_logged,
                        )


def sorted_scandir(base_path: str, sub_path: str, sort: bool = False) -> list[FileSystemItem]:
    """
    Implement os.scandir that returns (optionally) sorted entries.

    Not async friendly!
    """

    def nat_key(name: str) -> tuple[int | str, ...]:
        """Sort key for natural sorting, case insensitive to match the frontend sorting."""
        return tuple(int(s) if s.isdigit() else s.casefold() for s in re.split(r"(\d+)", name))

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
            if (
                entry.name in IGNORE_DIRS
                or entry.name.startswith(".")
                or _skip_undecodable_name(entry.name, logger)
            ):
                continue
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


def _skip_undecodable_name(name: str, log: logging.Logger) -> bool:
    """
    Return True if the given filename is not valid UTF-8 and must be skipped.

    A skipped name is logged in escaped form, so the caller only has to skip it.

    :param name: Name of the file or directory, as returned by the os module.
    :param log: Logger to report a skipped name on.
    """
    # such a path can be neither stored in the database nor sent to a client
    if name.isascii():
        return False
    if (safe_name := make_utf8_safe(name)) == name:
        return False
    log.warning("Skipping '%s' - filename is not valid UTF-8", safe_name)
    return True


def _record_entry_failure(
    scan_errors: ScanErrors,
    err: OSError,
    *,
    entry_path: str,
    base_path: str,
    log: logging.Logger,
    already_logged: bool,
) -> bool:
    """Register a directory entry that could not be read and report it once per directory."""
    # a share that drops mid-listing fails every entry in the directory it was reading,
    # so only the first one is a warning and the rest are debug to keep the log readable
    log.log(
        logging.DEBUG if already_logged else logging.WARNING,
        "Skipping %s due to OS error: %s",
        entry_path,
        err,
    )
    scan_errors.record_entry_error(err, get_relative_path(base_path, entry_path))
    return True


def _record_dir_failure(
    scan_errors: ScanErrors,
    err: OSError,
    *,
    path: str,
    base_path: str,
    log: logging.Logger,
) -> None:
    """Register a directory that could not be read and report it if the scan gives up."""
    is_root = path == base_path
    # a folder we may not read is an ACL problem; the storage itself is still there
    denied = err.errno in (errno.EACCES, errno.EPERM)
    scan_errors.record_dir_error(
        err,
        is_root=is_root,
        counts_toward_abort=not denied,
        path=get_relative_path(base_path, path),
    )
    if scan_errors.aborted and not is_root:
        log.error(
            "Stopping the scan of %s: %d folders in a row could not be read",
            base_path,
            scan_errors.consecutive_failures,
        )
