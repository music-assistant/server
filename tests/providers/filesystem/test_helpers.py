"""Tests for utility/helper functions."""

import errno
import logging
import os
from pathlib import Path
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
