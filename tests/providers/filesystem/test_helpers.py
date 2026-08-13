"""Tests for utility/helper functions."""

import errno
import logging
import os
from pathlib import Path
from typing import Self
from unittest.mock import patch

import pytest

from music_assistant.providers.filesystem_local import helpers

# ruff: noqa: S108


def test_get_artist_dir() -> None:
    """Test the extraction of an artist dir."""
    album_path = "/tmp/Artist/Album"
    artist_name = "Artist"
    assert helpers.get_artist_dir(artist_name, album_path) == "/tmp/Artist"
    album_path = "/tmp/artist/Album"
    assert helpers.get_artist_dir(artist_name, album_path) == "/tmp/artist"
    album_path = "/tmp/Album"
    assert helpers.get_artist_dir(artist_name, album_path) is None
    album_path = "/tmp/ARTIST!/Album"
    assert helpers.get_artist_dir(artist_name, album_path) == "/tmp/ARTIST!"
    album_path = "/tmp/Artist/Album"
    artist_name = "Artist!"
    assert helpers.get_artist_dir(artist_name, album_path) == "/tmp/Artist"
    album_path = "/tmp/REM/Album"
    artist_name = "R.E.M."
    assert helpers.get_artist_dir(artist_name, album_path) == "/tmp/REM"
    album_path = "/tmp/ACDC/Album"
    artist_name = "AC/DC"
    assert helpers.get_artist_dir(artist_name, album_path) == "/tmp/ACDC"
    album_path = "/tmp/Celine Dion/Album"
    artist_name = "Céline Dion"
    assert helpers.get_artist_dir(artist_name, album_path) == "/tmp/Celine Dion"
    album_path = "/tmp/Antonin Dvorak/Album"
    artist_name = "Antonín Dvořák"
    assert helpers.get_artist_dir(artist_name, album_path) == "/tmp/Antonin Dvorak"


@pytest.mark.parametrize(
    ("album_name", "track_dir", "expected"),
    [
        # Test literal match
        (
            "Selected Ambient Works 85-92",
            "/home/user/Music/Aphex Twin/Selected Ambient Works 85-92",
            "/home/user/Music/Aphex Twin/Selected Ambient Works 85-92",
        ),
        # Test artist - album format
        (
            "Selected Ambient Works 85-92",
            "/home/user/Music/Aphex Twin - Selected Ambient Works 85-92",
            "/home/user/Music/Aphex Twin - Selected Ambient Works 85-92",
        ),
        # Test artist - album (version) format
        (
            "Selected Ambient Works 85-92",
            "/home/user/Music/Aphex Twin - Selected Ambient Works 85-92 (Remastered)",
            "/home/user/Music/Aphex Twin - Selected Ambient Works 85-92 (Remastered)",
        ),
        # Test artist - album (version) format
        (
            "Selected Ambient Works 85-92",
            "/home/user/Music/Aphex Twin - Selected Ambient Works 85-92 (Remastered) - WEB",
            "/home/user/Music/Aphex Twin - Selected Ambient Works 85-92 (Remastered) - WEB",
        ),
        # Test tokenizer - dirname with extras
        (
            "Fokus - Prewersje",
            "/home/user/Fokus-Prewersje-PL-WEB-FLAC-2021-PS_INT",
            "/home/user/Fokus-Prewersje-PL-WEB-FLAC-2021-PS_INT",
        ),
        # Test tokenizer - dirname with version and extras
        (
            "Layo And Bushwacka - Night Works",
            "/home/music/Layo_And_Bushwacka-Night_Works_(Reissue)-(XLCD_154X)-FLAC-2003",
            "/home/music/Layo_And_Bushwacka-Night_Works_(Reissue)-(XLCD_154X)-FLAC-2003",
        ),
        # Test tokenizer - extras and approximate match on diacratics
        (
            "Łona i Webber - Wyślij Sobie Pocztówkę",
            "/usr/others/Lona-Discography-PL-FLAC-2020-INT/Lona_I_Webber-Wyslij_Sobie_Pocztowke-PL-WEB-FLAC-2014-PS",
            "/usr/others/Lona-Discography-PL-FLAC-2020-INT/Lona_I_Webber-Wyslij_Sobie_Pocztowke-PL-WEB-FLAC-2014-PS",
        ),
        (
            "NIC",
            "/nas/downloads/others/Sokol-NIC-PL-WEB-FLAC-2021",
            "/nas/downloads/others/Sokol-NIC-PL-WEB-FLAC-2021",
        ),
        # Test album (version) format
        (
            "Aphex Twin - Selected Ambient Works 85-92",
            "/home/user/Music/Aphex Twin/Selected Ambient Works 85-92 (Remastered)",
            "/home/user/Music/Aphex Twin/Selected Ambient Works 85-92 (Remastered)",
        ),
        # Test album name in dir
        (
            "Aphex Twin - Selected Ambient Works 85-92",
            "/home/user/Music/RandomDirWithAphex Twin - Selected Ambient Works 85-92InIt",
            "/home/user/Music/RandomDirWithAphex Twin - Selected Ambient Works 85-92InIt",
        ),
        # Test no match
        (
            "NonExistentAlbumName",
            "/home/user/Music/Aphex Twin/Selected Ambient Works 85-92",
            None,
        ),
        # Test empty album name
        ("", "/home/user/Music/Aphex Twin/Selected Ambient Works 85-92", None),
        # Test empty track dir
        ("Selected Ambient Works 85-92", "", None),
    ],
)
def test_get_album_dir(album_name: str, track_dir: str, expected: str) -> None:
    """Test the extraction of an album dir."""
    assert helpers.get_album_dir(track_dir, album_name) == expected


SUPPORTED = {"mp3", "flac"}


def _build_music_tree(root: Path) -> None:
    """Create a small music tree fixture."""
    (root / "Artist1" / "Album1").mkdir(parents=True)
    (root / "Artist1" / "Album1" / "track1.mp3").write_bytes(b"x")
    (root / "Artist1" / "Album1" / "track2.flac").write_bytes(b"x")
    (root / "Artist2").mkdir()
    (root / "Artist2" / "track3.mp3").write_bytes(b"x")


def test_recursive_iter_happy_path(tmp_path: Path) -> None:
    """Test that a healthy scan yields all supported files and records no errors."""
    _build_music_tree(tmp_path)
    errors = helpers.ScanErrors()
    items = list(
        helpers.recursive_iter(
            str(tmp_path),
            str(tmp_path),
            SUPPORTED,
            logging.getLogger("test"),
            errors,
        )
    )
    rel_paths = sorted(i.relative_path for i in items)
    assert rel_paths == [
        "Artist1/Album1/track1.mp3",
        "Artist1/Album1/track2.flac",
        "Artist2/track3.mp3",
    ]
    assert not errors.fatal
    assert errors.failed_dirs == 0


def test_recursive_iter_root_unreachable_records_error(tmp_path: Path) -> None:
    """Test that a missing root path is reported via scan_errors."""
    errors = helpers.ScanErrors()
    missing = tmp_path / "does-not-exist"
    items = list(
        helpers.recursive_iter(
            str(missing),
            str(missing),
            SUPPORTED,
            logging.getLogger("test"),
            errors,
        )
    )
    assert items == []
    assert isinstance(errors.fatal, OSError)
    assert errors.fatal.errno == errno.ENOENT


def test_recursive_iter_root_eacces_records_error() -> None:
    """Test that permission-denied on the root path is reported via scan_errors."""
    errors = helpers.ScanErrors()
    with patch("os.scandir", side_effect=PermissionError(errno.EACCES, "denied")):
        items = list(
            helpers.recursive_iter(
                "/fake/root",
                "/fake/root",
                SUPPORTED,
                logging.getLogger("test"),
                errors,
            )
        )
    assert items == []
    assert isinstance(errors.fatal, OSError)
    assert errors.fatal.errno == errno.EACCES


def test_recursive_iter_subfolder_failure_is_not_fatal(tmp_path: Path) -> None:
    """Test that a single sub-folder scan failure is not fatal."""
    _build_music_tree(tmp_path)
    errors = helpers.ScanErrors()
    real_scandir = os.scandir
    bad_dir = str(tmp_path / "Artist1" / "Album1")

    def fake_scandir(path: str | os.PathLike[str]):  # type: ignore[no-untyped-def]
        if str(path) == bad_dir:
            raise OSError(errno.EIO, "i/o error")
        return real_scandir(path)

    with patch("os.scandir", side_effect=fake_scandir):
        items = list(
            helpers.recursive_iter(
                str(tmp_path),
                str(tmp_path),
                SUPPORTED,
                logging.getLogger("test"),
                errors,
            )
        )

    rel_paths = sorted(i.relative_path for i in items)
    assert rel_paths == ["Artist2/track3.mp3"]
    assert not errors.fatal
    # the scan is incomplete, so callers must not run deletions
    assert errors.failed_dirs == 1


def test_recursive_iter_einval_is_ignored() -> None:
    """Test that EINVAL from an unsupported path name is not recorded."""
    errors = helpers.ScanErrors()
    with patch("os.scandir", side_effect=OSError(errno.EINVAL, "invalid path")):
        items = list(
            helpers.recursive_iter(
                "/weird/\udcff",
                "/weird/\udcff",
                SUPPORTED,
                logging.getLogger("test"),
                errors,
            )
        )
    assert items == []
    assert not errors.fatal
    assert errors.failed_dirs == 0


def _build_flat_tree(root: Path, count: int) -> None:
    """Create a music tree with the given number of album folders."""
    for index in range(count):
        album_dir = root / f"Album{index:03d}"
        album_dir.mkdir()
        (album_dir / "track.mp3").write_bytes(b"x")


def test_recursive_iter_aborts_after_consecutive_failures(tmp_path: Path) -> None:
    """Test that storage disappearing mid-scan aborts the walk instead of grinding on."""
    _build_flat_tree(tmp_path, helpers.MAX_CONSECUTIVE_SCAN_ERRORS + 10)
    errors = helpers.ScanErrors()
    real_scandir = os.scandir

    def fake_scandir(path: str | os.PathLike[str]):  # type: ignore[no-untyped-def]
        if str(path) == str(tmp_path):
            return real_scandir(path)
        raise OSError(errno.EIO, "i/o error")

    with patch("os.scandir", side_effect=fake_scandir):
        items = list(
            helpers.recursive_iter(
                str(tmp_path),
                str(tmp_path),
                SUPPORTED,
                logging.getLogger("test"),
                errors,
            )
        )

    assert items == []
    assert errors.aborted
    # the walk stopped at the threshold instead of trying every remaining folder
    assert errors.failed_dirs == helpers.MAX_CONSECUTIVE_SCAN_ERRORS


def test_recursive_iter_einval_does_not_abort(tmp_path: Path) -> None:
    """Test that skipped (unsupported) path names never trip the abort threshold."""
    _build_flat_tree(tmp_path, helpers.MAX_CONSECUTIVE_SCAN_ERRORS + 10)
    (tmp_path / "root.mp3").write_bytes(b"x")
    errors = helpers.ScanErrors()
    real_scandir = os.scandir

    def fake_scandir(path: str | os.PathLike[str]):  # type: ignore[no-untyped-def]
        if str(path) == str(tmp_path):
            return real_scandir(path)
        raise OSError(errno.EINVAL, "invalid argument")

    with patch("os.scandir", side_effect=fake_scandir):
        items = list(
            helpers.recursive_iter(
                str(tmp_path),
                str(tmp_path),
                SUPPORTED,
                logging.getLogger("test"),
                errors,
            )
        )

    assert [item.relative_path for item in items] == ["root.mp3"]
    assert not errors.aborted
    assert errors.failed_dirs == 0


def test_recursive_iter_permission_denied_does_not_abort(tmp_path: Path) -> None:
    """Test that ACL-protected folders leave the scan incomplete without aborting it."""
    _build_flat_tree(tmp_path, helpers.MAX_CONSECUTIVE_SCAN_ERRORS + 10)
    (tmp_path / "root.mp3").write_bytes(b"x")
    errors = helpers.ScanErrors()
    real_scandir = os.scandir

    def fake_scandir(path: str | os.PathLike[str]):  # type: ignore[no-untyped-def]
        if str(path) == str(tmp_path):
            return real_scandir(path)
        raise PermissionError(errno.EACCES, "denied")

    with patch("os.scandir", side_effect=fake_scandir):
        items = list(
            helpers.recursive_iter(
                str(tmp_path),
                str(tmp_path),
                SUPPORTED,
                logging.getLogger("test"),
                errors,
            )
        )

    assert [item.relative_path for item in items] == ["root.mp3"]
    assert not errors.aborted
    assert errors.consecutive_failures == 0
    # the folders were still missed, so callers must not run deletions
    assert errors.failed_dirs == helpers.MAX_CONSECUTIVE_SCAN_ERRORS + 10


class _BrokenEntry:
    """Directory entry whose type check fails, as on a share that drops mid-listing."""

    def __init__(self, path: str, err: OSError) -> None:
        self.name = Path(path).name
        self.path = path
        self._err = err

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        """Raise the configured error instead of answering."""
        raise self._err

    def is_file(self, follow_symlinks: bool = True) -> bool:
        """Raise the configured error instead of answering."""
        raise self._err


class _FakeScanDir:
    """Stand-in for the os.scandir iterator, which is also a context manager."""

    def __init__(self, entries: list[_BrokenEntry]) -> None:
        self._entries = iter(entries)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> _BrokenEntry:
        return next(self._entries)


def test_recursive_iter_unreadable_file_is_recorded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that files that cannot be read leave the scan incomplete."""
    _build_music_tree(tmp_path)
    errors = helpers.ScanErrors()
    real_from_dir_entry = helpers.FileSystemItem.from_dir_entry

    def fake_from_dir_entry(entry: os.DirEntry[str], base_path: str) -> helpers.FileSystemItem:
        if entry.name.startswith("track1") or entry.name.startswith("track2"):
            raise OSError(errno.EIO, "i/o error")
        return real_from_dir_entry(entry, base_path)

    with (
        caplog.at_level(logging.DEBUG, logger="test"),
        patch.object(helpers.FileSystemItem, "from_dir_entry", fake_from_dir_entry),
    ):
        items = list(
            helpers.recursive_iter(
                str(tmp_path),
                str(tmp_path),
                SUPPORTED,
                logging.getLogger("test"),
                errors,
            )
        )

    assert [item.relative_path for item in items] == ["Artist2/track3.mp3"]
    assert not errors.aborted
    assert errors.failed_dirs == 0
    # the files are still on disk, so callers must not run deletions
    assert errors.failed_entries == 2
    assert errors.incomplete
    # the summary names them so the user does not need the log to find them
    assert "Artist1/Album1/track1.mp3" in errors.describe()
    # both files failed in the same folder, so only the first one is a warning
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.parametrize("err", [OSError(errno.ENOENT, "gone"), OSError(errno.EINVAL, "invalid")])
def test_recursive_iter_vanished_file_is_ignored(tmp_path: Path, err: OSError) -> None:
    """Test that a file that is really gone does not block deletions."""
    _build_music_tree(tmp_path)
    errors = helpers.ScanErrors()
    real_from_dir_entry = helpers.FileSystemItem.from_dir_entry

    def fake_from_dir_entry(entry: os.DirEntry[str], base_path: str) -> helpers.FileSystemItem:
        if entry.name == "track1.mp3":
            raise err
        return real_from_dir_entry(entry, base_path)

    with patch.object(helpers.FileSystemItem, "from_dir_entry", fake_from_dir_entry):
        items = list(
            helpers.recursive_iter(
                str(tmp_path),
                str(tmp_path),
                SUPPORTED,
                logging.getLogger("test"),
                errors,
            )
        )

    assert "Artist1/Album1/track1.mp3" not in [item.relative_path for item in items]
    assert not errors.incomplete


def test_recursive_iter_unreadable_entry_type_is_recorded(tmp_path: Path) -> None:
    """Test that an entry of unknown type leaves the scan incomplete."""
    errors = helpers.ScanErrors()
    entries = [_BrokenEntry(str(tmp_path / "Album1"), OSError(errno.EIO, "i/o error"))]

    with patch("os.scandir", return_value=_FakeScanDir(entries)):
        items = list(
            helpers.recursive_iter(
                str(tmp_path),
                str(tmp_path),
                SUPPORTED,
                logging.getLogger("test"),
                errors,
            )
        )

    assert items == []
    assert not errors.aborted
    # the entry may be a folder full of tracks, so callers must not run deletions
    assert errors.failed_entries == 1


def test_scan_errors_describe_names_examples() -> None:
    """Test that the summary names the failed paths it kept."""
    errors = helpers.ScanErrors()
    errors.record_dir_error(OSError(errno.EIO, "i/o error"), is_root=False, path="Artist1/Album1")
    for index in range(helpers.MAX_REPORTED_FAILED_PATHS + 5):
        errors.record_entry_error(OSError(errno.EIO, "i/o error"), f"Artist2/track{index}.mp3")

    summary = errors.describe()
    assert "1 folder(s)" in summary
    assert f"{helpers.MAX_REPORTED_FAILED_PATHS + 5} file(s)" in summary
    assert "Artist1/Album1" in summary
    # only the first few paths are named, the counts carry the rest
    assert len(errors.failed_paths) == helpers.MAX_REPORTED_FAILED_PATHS


def test_recursive_iter_skips_names_that_are_not_valid_utf8(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Test that a filename which is not valid UTF-8 is skipped, naming it escaped.

    Its path can be neither stored nor serialized, so letting it through only fails
    deeper down, taking the events to the clients and the settings file with it
    (#6042). Emoji are valid UTF-8 and must keep scanning.
    """
    (tmp_path / "track 🎧.mp3").write_bytes(b"x")
    # 0xDF is "ß" in Latin-1 and not valid UTF-8, so os returns it surrogate-escaped
    undecodable = os.path.join(os.fsencode(str(tmp_path)), b"Stra\xdfe.mp3")
    with open(undecodable, "wb") as _file:
        _file.write(b"x")

    errors = helpers.ScanErrors()
    with caplog.at_level(logging.WARNING):
        items = list(
            helpers.recursive_iter(
                str(tmp_path), str(tmp_path), SUPPORTED, logging.getLogger("test"), errors
            )
        )

    assert [item.relative_path for item in items] == ["track 🎧.mp3"]
    assert "Stra\\xdfe.mp3" in caplog.text
    # such a file can never have been indexed, so skipping it must not block deletions
    assert not errors.incomplete


def test_scan_errors_reset_on_successful_read() -> None:
    """Test that a directory read in between failures resets the abort threshold."""
    errors = helpers.ScanErrors()
    err = OSError(errno.EIO, "i/o error")
    for _ in range(helpers.MAX_CONSECUTIVE_SCAN_ERRORS - 1):
        errors.record_dir_error(err, is_root=False)
    assert not errors.aborted

    errors.record_dir_read()
    assert errors.consecutive_failures == 0

    for _ in range(helpers.MAX_CONSECUTIVE_SCAN_ERRORS - 1):
        errors.record_dir_error(err, is_root=False)
    assert not errors.aborted
    assert errors.failed_dirs == (helpers.MAX_CONSECUTIVE_SCAN_ERRORS - 1) * 2
