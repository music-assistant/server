"""Tests for utility/helper functions."""

import errno
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Self
from unittest.mock import patch

import pytest

from music_assistant.helpers.compare import compare_strings
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


def test_get_album_dir_falls_back_to_sort_name_alias() -> None:
    """When the plain album name does not match a folder, the sort-name alias is tried."""
    track_dir = "/tmp/Artist/Wall, The"
    assert helpers.get_album_dir(track_dir, "The Wall") is None
    assert helpers.get_album_dir(track_dir, "The Wall", album_sort="Wall, The") == track_dir
    # the plain name still wins when it matches, without needing the alias
    assert helpers.get_album_dir("/tmp/Artist/The Wall", "The Wall", album_sort="Wall, The") == (
        "/tmp/Artist/The Wall"
    )


def test_get_album_dir_plain_name_at_a_farther_level_outranks_a_nearer_sort_name_alias() -> None:
    """
    A nearer sort-name alias match must never outrank a farther, exact plain-name match.

    Both levels are searched for the plain album name first; the sort-name alias is only
    tried afterwards, and only if the plain name matched nowhere.
    """
    # "Wall, The" (the sort-name alias) is the track's own directory, one level nearer than
    # "The Wall" (the plain, exact name) at its parent
    track_dir = "/tmp/Artist/The Wall/Wall, The"
    assert (
        helpers.get_album_dir(track_dir, "The Wall", album_sort="Wall, The")
        == "/tmp/Artist/The Wall"
    )


def test_get_artist_dir_falls_back_to_sort_name_alias() -> None:
    """When the plain artist name does not match a folder, the sort-name alias is tried."""
    album_path = "/tmp/Beatles, The/Album"
    assert helpers.get_artist_dir("The Beatles", album_path) is None
    assert helpers.get_artist_dir("The Beatles", album_path, sort_name="Beatles, The") == (
        "/tmp/Beatles, The"
    )


def test_get_artist_dir_plain_name_outranks_a_farther_sort_name_alias() -> None:
    """
    A farther sort-name alias match must never outrank a nearer, exact plain-name match.

    The plain artist name's own bounded (up to 3 ancestor levels) search completes in full
    before the sort-name alias is tried at all.
    """
    # "The Beatles" (the plain, exact name) is the immediate parent; "Beatles, The" (the
    # sort-name alias) is one level further up
    album_path = "/tmp/Beatles, The/The Beatles/Album"
    assert (
        helpers.get_artist_dir("The Beatles", album_path, sort_name="Beatles, The")
        == "/tmp/Beatles, The/The Beatles"
    )


def test_get_artist_dir_exact_only_ignores_the_sort_name_alias_and_fuzzy_matches() -> None:
    """`exact_only` skips the sort-name alias and the relaxed (fuzzy) comparison entirely."""
    # the sort-name alias itself is a relaxed heuristic and must not be tried
    album_path = "/tmp/Beatles, The/Album"
    assert (
        helpers.get_artist_dir("The Beatles", album_path, sort_name="Beatles, The", exact_only=True)
        is None
    )
    # a near-miss that only the fuzzy (non-strict) comparison would accept must not match
    album_path = "/tmp/The Beetles/Album"
    assert helpers.get_artist_dir("The Beatles", album_path, exact_only=True) is None
    assert helpers.get_artist_dir("The Beatles", album_path) == "/tmp/The Beetles"
    # an exact (normalized) match still succeeds
    album_path = "/tmp/The Beatles/Album"
    assert helpers.get_artist_dir("The Beatles", album_path, exact_only=True) == (
        "/tmp/The Beatles"
    )


@pytest.mark.parametrize(
    ("dirname", "expected"),
    [
        ("2025-03-14 Vaxis Act III The Father of Make Believe", True),
        ("2025.03.14 Vaxis Act III The Father of Make Believe", True),
        ("1995-03-13 Vaxis Act III The Father of Make Believe", True),
        ("2025-03-14 VAXIS ACT III THE FATHER OF MAKE BELIEVE", True),
        ("(2025) Vaxis Act III The Father of Make Believe", True),
        ("[2025] Vaxis Act III The Father of Make Believe", True),
        ("2025 Vaxis Act III The Father of Make Believe", True),
        ("Vaxis Act III The Father of Make Believe", True),
    ],
)
def test_dir_matches_album_strips_a_recognized_date_prefix(dirname: str, expected: bool) -> None:
    """A leading release date/year, in any recognized format or case, is not part of the title."""
    assert helpers._dir_matches_album(dirname, "Vaxis Act III: The Father of Make Believe") is (
        expected
    )


def test_dir_matches_album_date_prefixed_king_for_a_day_example() -> None:
    """The second #3994 reproduction: a date-prefixed folder with an ellipsis-free title."""
    assert helpers._dir_matches_album(
        "1995-03-13 King for a Day Fool for a Lifetime",
        "King for a Day... Fool for a Lifetime",
    )


def test_dir_matches_album_king_for_a_day_example_without_date_prefix() -> None:
    """The #3994 examples without a date prefix already matched before this change too."""
    assert helpers._dir_matches_album(
        "King for a Day Fool for a Lifetime",
        "King for a Day... Fool for a Lifetime",
    )


def test_strip_date_prefix_does_not_touch_an_arbitrary_catalogue_prefix() -> None:
    """A catalogue prefix like "CAT-1234" does not start with a 4-digit date/year at all."""
    assert helpers._strip_date_prefix("CAT-1234 Album Name") == "CAT-1234 Album Name"


def test_strip_date_prefix_requires_a_real_separator_after_the_year() -> None:
    """A bare year glued directly onto the title (no separator) is not stripped."""
    assert helpers._strip_date_prefix("2025Album") == "2025Album"


def test_dir_matches_album_date_prefix_path_rejects_reordered_words() -> None:
    """The new date-prefix comparison is strict normalized equality, not token matching."""
    stripped = helpers._strip_date_prefix("2025-03-14 Beta Alpha")
    assert stripped == "Beta Alpha"
    assert compare_strings("Alpha Beta", stripped, True) is False


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Disc 1", True),
        ("disc1", True),
        ("CD2", True),
        ("cd 03", True),
        ("Disk 1", True),
        ("DVD1", True),
        ("Volume 2", True),
        ("Vol. 2", True),
        ("Album", False),
        ("weird-disc-name", False),
        ("", False),
    ],
)
def test_is_disc_dir(name: str, expected: bool) -> None:
    """Only a recognized disc/volume naming pattern is treated as a disc subfolder."""
    assert helpers.is_disc_dir(name) is expected


def test_parse_nfo_root_returns_the_named_root_element() -> None:
    """A well-formed NFO with the expected root element returns it as a dict."""
    data = b"<album><title>My Album</title></album>"
    root = helpers.parse_nfo_root(data, "album")
    assert root is not None
    assert root["title"] == "My Album"


@pytest.mark.parametrize(
    ("data", "root_tag"),
    [
        (b"not xml at all <<<", "album"),
        (b"<artist><title>Name</title></artist>", "album"),  # wrong root element
        (b"\xff\xfe not utf-8", "album"),
        (b"<album>just text, no dict</album>", "album"),
    ],
)
def test_parse_nfo_root_returns_none_for_malformed_content(data: bytes, root_tag: str) -> None:
    """Malformed XML, an undecodable file or the wrong/non-dict root element returns None."""
    assert helpers.parse_nfo_root(data, root_tag) is None


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


class _NamedEntry:
    """Directory entry carrying only a name, for names a filesystem may refuse to create."""

    def __init__(self, parent: str, name: str) -> None:
        self.name = name
        self.path = os.path.join(parent, name)

    # no is_dir/is_file on purpose: the name guard has to skip this entry before anything
    # reads its type, so a call site that lost the guard fails loudly here instead of
    # quietly dropping the entry and leaving the test green
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"'{self.name}' must be skipped on its name, before .{name}")


_ScanEntry = _BrokenEntry | _NamedEntry | os.DirEntry[str]


class _FakeScanDir:
    """Stand-in for the os.scandir iterator, which is also a context manager."""

    def __init__(self, entries: Sequence[_ScanEntry]) -> None:
        self._entries = iter(entries)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> _ScanEntry:
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


# 0xDF is "ß" in Latin-1 and not valid UTF-8. os.fsdecode is what os.scandir uses to build
# DirEntry.name, so this is exactly what a real scan hands the guard for such a file, while
# needing no file on disk - filesystems that enforce UTF-8 names refuse to create one.
UNDECODABLE_NAME = os.fsdecode(b"Stra\xdfe.mp3")


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
    errors = helpers.ScanErrors()
    real_scandir = os.scandir

    def fake_scandir(path: str | os.PathLike[str]) -> _FakeScanDir:
        with real_scandir(path) as entries:
            return _FakeScanDir([*entries, _NamedEntry(str(path), UNDECODABLE_NAME)])

    with (
        caplog.at_level(logging.WARNING),
        patch("os.scandir", side_effect=fake_scandir),
    ):
        items = list(
            helpers.recursive_iter(
                str(tmp_path), str(tmp_path), SUPPORTED, logging.getLogger("test"), errors
            )
        )

    assert [item.relative_path for item in items] == ["track 🎧.mp3"]
    assert "Stra\\xdfe.mp3" in caplog.text
    # such a file can never have been indexed, so skipping it must not block deletions
    assert not errors.incomplete


def test_sorted_scandir_skips_names_that_are_not_valid_utf8(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Test that the directory listing skips a filename which is not valid UTF-8.

    This listing feeds browse, podcast episodes, playlist and folder images, audiobooks
    and chapters, so an item that can not be serialized would fail whichever of those
    asked for it (#6042).
    """
    (tmp_path / "track 🎧.mp3").write_bytes(b"x")
    real_scandir = os.scandir

    def fake_scandir(path: str | os.PathLike[str]) -> _FakeScanDir:
        with real_scandir(path) as entries:
            return _FakeScanDir([*entries, _NamedEntry(str(path), UNDECODABLE_NAME)])

    with (
        caplog.at_level(logging.WARNING),
        patch("os.scandir", side_effect=fake_scandir),
    ):
        items = helpers.sorted_scandir(str(tmp_path), str(tmp_path))

    assert [item.relative_path for item in items] == ["track 🎧.mp3"]
    assert "Stra\\xdfe.mp3" in caplog.text


def test_skip_undecodable_name_passes_valid_names(caplog: pytest.LogCaptureFixture) -> None:
    """Test that the guard passes valid names without warning, whatever their encoding."""
    log = logging.getLogger("test")
    with caplog.at_level(logging.WARNING):
        assert not helpers._skip_undecodable_name("track.mp3", log)
        assert not helpers._skip_undecodable_name("Straße.mp3", log)
        # 4-byte UTF-8 takes the same path as the 2-byte name above
        assert not helpers._skip_undecodable_name("track 🎧.mp3", log)

    assert not caplog.records


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
