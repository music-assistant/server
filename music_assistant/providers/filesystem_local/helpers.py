"""Some helpers for Filesystem based Musicproviders."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
from collections.abc import Hashable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from xml.parsers.expat import ExpatError

import xmltodict
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import MediaItemImage, UniqueList

from music_assistant.helpers.compare import compare_strings
from music_assistant.helpers.json import make_utf8_safe
from music_assistant.helpers.security import is_safe_path

from .constants import IMAGE_EXTENSIONS, NFO_SIDECAR_NAMES, RECOGNIZED_IMAGE_STEMS

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

_ScalarT = TypeVar("_ScalarT")
_HashableT = TypeVar("_HashableT", bound=Hashable)

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
    def parent_name(self) -> str:
        """Return the name of this item's parent directory."""
        # derived from the relative path: the absolute path may be a URL on
        # network/cloud providers (webdav, cloud filesystems)
        return Path(self.relative_parent_path).name

    @property
    def relative_parent_path(self) -> str:
        """Return relative parent path of this item."""
        return os.path.dirname(self.relative_path)

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
        )


def get_folder_signature(items: list[FileSystemItem]) -> str:
    """
    Return an order-independent digest of the given files' paths, mtimes and sizes.

    Intended as a cache checksum: any file added, removed, replaced or retagged changes it.

    :param items: The files to include in the digest.
    """
    parts = sorted(f"{x.relative_path}\0{x.checksum}\0{x.file_size}" for x in items)
    return hashlib.sha256("\0\0".join(parts).encode()).hexdigest()


def is_sidecar_file(item: FileSystemItem) -> bool:
    """Return True when item is a recognized music metadata sidecar (NFO or folder image)."""
    return not item.is_dir and (_is_image_sidecar(item) or _is_nfo_sidecar(item))


@dataclass
class SidecarIndex:
    """
    Recognized metadata sidecars and track-containing directories gathered during a music sync.

    Populated from the directory listings the walk already produces, so sidecar add/edit/remove is
    detectable without extra probes. ``album.nfo`` is only read from the album's own mapping
    directory (Kodi layout); an album's artwork spans its folder plus the immediate subfolders that
    actually contain its tracks (disc folders), excluding subfolders that are themselves mapped
    albums.
    """

    files_by_dir: dict[str, list[FileSystemItem]] = field(default_factory=dict)
    track_dirs: set[str] = field(default_factory=set)
    _track_children: dict[str, list[str]] | None = field(default=None, init=False, repr=False)

    def record(self, item: FileSystemItem) -> bool:
        """
        Record item if it is a recognized sidecar; return whether it was recorded.

        :param item: A file discovered while walking the provider tree.
        """
        if not is_sidecar_file(item):
            return False
        self.files_by_dir.setdefault(item.relative_parent_path, []).append(item)
        return True

    def record_track_dir(self, folder: str) -> None:
        """Record a directory that holds scanned audio/CUE content (a track or disc folder)."""
        if folder not in self.track_dirs:
            self.track_dirs.add(folder)
            self._track_children = None

    def image_items(self, folder: str) -> list[FileSystemItem]:
        """Return the recognized image sidecars recorded directly inside folder."""
        return [item for item in self.files_by_dir.get(folder, ()) if _is_image_sidecar(item)]

    def files(self, folder: str) -> list[FileSystemItem]:
        """Return all recognized sidecars recorded directly inside folder."""
        return list(self.files_by_dir.get(folder, ()))

    def nfo_item(self, folder: str, name: str) -> FileSystemItem | None:
        """Return the named NFO sidecar recorded directly inside folder, if present."""
        name = name.lower()
        for item in self.files_by_dir.get(folder, ()):
            if item.filename.lower() == name:
                return item
        return None

    def album_image_dirs(self, album_dir: str, mapped_album_dirs: set[str]) -> list[str]:
        """
        Return the folders an album draws artwork from.

        This is its own folder plus the immediate subfolders that hold its tracks, minus any that
        are themselves mapped albums.

        :param album_dir: The album's mapping directory.
        :param mapped_album_dirs: Directories that are known album mappings (excluded as discs).
        """
        dirs = [album_dir]
        dirs.extend(
            child for child in self._child_track_dirs(album_dir) if child not in mapped_album_dirs
        )
        return dirs

    def album_signatures(self, album_dir: str, mapped_album_dirs: set[str]) -> tuple[str, str]:
        """
        Return the ``(nfo_signature, image_signature)`` for an album mapped at album_dir.

        :param album_dir: The album's mapping directory.
        :param mapped_album_dirs: Directories that are known album mappings (excluded as discs).
        """
        nfo_item = self.nfo_item(album_dir, "album.nfo")
        images: list[FileSystemItem] = []
        for folder in self.album_image_dirs(album_dir, mapped_album_dirs):
            images.extend(self.image_items(folder))
        return (
            get_folder_signature([nfo_item] if nfo_item else []),
            get_folder_signature(images),
        )

    def artist_signatures(self, artist_path: str) -> tuple[str, str]:
        """
        Return the ``(nfo_signature, image_signature)`` for an artist mapped at artist_path.

        :param artist_path: The artist's mapping directory.
        """
        nfo_item = self.nfo_item(artist_path, "artist.nfo")
        return (
            get_folder_signature([nfo_item] if nfo_item else []),
            get_folder_signature(list(self.image_items(artist_path))),
        )

    def _child_track_dirs(self, parent: str) -> list[str]:
        """Return the immediate child directories of parent that hold tracks (memoized, O(1))."""
        if self._track_children is None:
            children: dict[str, list[str]] = {}
            for track_dir in self.track_dirs:
                children.setdefault(os.path.dirname(track_dir), []).append(track_dir)
            self._track_children = children
        return self._track_children.get(parent, [])


class SidecarReadError(Exception):
    """Raised when a sidecar or representative track cannot be read due to a transient failure."""


def reconcile_scalar(
    stored: _ScalarT | None, fresh: _ScalarT | None, previous: _ScalarT | None
) -> _ScalarT | None:
    """
    Return the value to keep for a provider-managed scalar during a sidecar refresh.

    The freshly parsed value wins. When the sidecar no longer provides one, the stored value is
    cleared only if it still equals what this provider last contributed, so another provider's
    value (or a manual edit) is preserved.

    :param stored: The value currently held by the library item.
    :param fresh: The value the sidecar provides now, or None when absent.
    :param previous: The value this provider last contributed, or None when unknown.
    """
    if fresh is not None:
        return fresh
    if previous is not None and stored == previous:
        return None
    return stored


def reconcile_provenance_set(
    stored: Iterable[_HashableT] | None,
    fresh_nfo: Iterable[_HashableT] | None,
    previous_nfo: Iterable[_HashableT] | None,
) -> set[_HashableT]:
    """
    Reconcile a set the sidecar contributes to (genres, external ids) by provenance.

    Removes exactly what this provider's NFO previously contributed and adds what it contributes
    now, leaving values from the audio tags or other providers untouched. Removing one NFO value
    therefore keeps the rest.

    :param stored: The set currently held by the library item.
    :param fresh_nfo: The values this NFO contributes now (empty when the NFO is gone).
    :param previous_nfo: The values this NFO contributed last time.
    """
    return (set(stored or ()) - set(previous_nfo or ())) | set(fresh_nfo or ())


def reconcile_images(
    stored: Iterable[MediaItemImage] | None,
    fresh_provider_images: Iterable[MediaItemImage],
    provider_instance: str,
) -> UniqueList[MediaItemImage]:
    """
    Merge freshly parsed provider images with images owned by other providers.

    Images previously contributed by this provider instance are dropped and replaced by the fresh
    set, so a removed local image disappears while other providers' images are kept.

    :param stored: Images currently on the library item.
    :param fresh_provider_images: Images parsed from this provider's folder(s) now.
    :param provider_instance: This provider's instance id (the image provenance key).
    """
    kept = [image for image in (stored or ()) if image.provider != provider_instance]
    return UniqueList([*kept, *fresh_provider_images])


def nfo_root_dict(
    raw: str, root: str, source: str, log: logging.Logger
) -> Mapping[str, Any] | None:
    """
    Parse an NFO document and return its root element as a mapping, or None if it is unusable.

    Guards against malformed XML and wrong, empty or scalar roots (``<foo/>``, ``<album/>``,
    ``<album>text</album>``, a repeated root parsed as a list) so a bad sidecar is logged and
    ignored instead of aborting the scan.

    :param raw: The NFO file contents.
    :param root: The expected root element name (``album`` or ``artist``).
    :param source: The NFO path, named in the warning.
    :param log: Logger for the warning.
    """
    try:
        parsed = xmltodict.parse(raw)
    except ExpatError as err:
        log.warning("Ignoring malformed NFO file %s: %s", source, err)
        return None
    node = parsed.get(root) if isinstance(parsed, dict) else None
    if not isinstance(node, dict):
        log.warning("Ignoring NFO file %s: missing or invalid <%s> root element", source, root)
        return None
    return node


def _is_image_sidecar(item: FileSystemItem) -> bool:
    """Return True when item is a recognized folder image."""
    return item.ext in IMAGE_EXTENSIONS and item.name.lower() in RECOGNIZED_IMAGE_STEMS


def _is_nfo_sidecar(item: FileSystemItem) -> bool:
    """Return True when item is a recognized NFO sidecar."""
    return item.filename.lower() in NFO_SIDECAR_NAMES


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
        dirname = Path(parentdir).name
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


def get_album_dir(track_dir: str, album_name: str) -> str | None:
    """Return album/parent directory of a track."""
    parentdir = track_dir
    # account for disc sublevel by ignoring 1 level if needed
    for _ in range(2):
        dirname = Path(parentdir).name
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
