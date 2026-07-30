"""Tests for the filesystem provider helpers."""

from pathlib import Path

from music_assistant.providers.filesystem_local.helpers import sorted_scandir


def test_sorted_scandir_natural_order(tmp_path: Path) -> None:
    """Entries are returned in natural, case insensitive order when sort is enabled."""
    (tmp_path / "Incoming").mkdir()
    (tmp_path / "albums").mkdir()
    for name in ("10 - Third.flac", "2 - Second.flac", "1 - First.flac", "cover.jpg"):
        (tmp_path / name).touch()

    result = sorted_scandir(str(tmp_path), str(tmp_path), sort=True)

    assert [item.filename for item in result] == [
        "1 - First.flac",
        "2 - Second.flac",
        "10 - Third.flac",
        "albums",
        "cover.jpg",
        "Incoming",
    ]


def test_sorted_scandir_unsorted_by_default(tmp_path: Path) -> None:
    """Without the sort flag, entries are returned in raw scandir order."""
    for name in ("b.flac", "a.flac"):
        (tmp_path / name).touch()

    result = sorted_scandir(str(tmp_path), str(tmp_path))

    assert sorted(item.filename for item in result) == ["a.flac", "b.flac"]
