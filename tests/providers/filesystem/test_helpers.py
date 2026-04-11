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
    errors: list[OSError] = []
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
    assert errors == []


def test_recursive_iter_root_unreachable_records_error(tmp_path: Path) -> None:
    """Test that a missing root path is reported via scan_errors."""
    errors: list[OSError] = []
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
    assert len(errors) == 1
    assert errors[0].errno == errno.ENOENT


def test_recursive_iter_root_eacces_records_error() -> None:
    """Test that permission-denied on the root path is reported via scan_errors."""
    errors: list[OSError] = []
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
    assert len(errors) == 1
    assert errors[0].errno == errno.EACCES


def test_recursive_iter_subfolder_failure_is_not_fatal(tmp_path: Path) -> None:
    """Test that a sub-folder scan failure does not populate scan_errors."""
    _build_music_tree(tmp_path)
    errors: list[OSError] = []
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
    assert errors == []


def test_recursive_iter_einval_is_ignored() -> None:
    """Test that EINVAL from an unsupported path name is not recorded."""
    errors: list[OSError] = []
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
    assert errors == []
