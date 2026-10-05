"""Tests for the filesystem provider helpers."""

from pathlib import Path

from music_assistant.providers.filesystem_local.helpers import (
    FileSystemItem,
    get_folder_signature,
    sorted_scandir,
)


def _file_item(name: str, checksum: str = "1700000000", file_size: int = 1024) -> FileSystemItem:
    """
    Build a FileSystemItem for a file, without touching the filesystem.

    :param name: Relative path of the file.
    :param checksum: Last modified time of the file.
    :param file_size: Size of the file in bytes.
    """
    return FileSystemItem(
        filename=name.rsplit("/", 1)[-1],
        relative_path=name,
        absolute_path=f"/media/{name}",
        is_dir=False,
        checksum=checksum,
        file_size=file_size,
    )


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


def test_sorted_scandir_handles_non_decimal_digits(tmp_path: Path) -> None:
    """Names with digit-like characters that int() rejects still sort without raising."""
    names = (
        "Cherry Moon - The Compilation 2002\u00b3",
        "Cherry Moon - The Compilation 2001",
        "\u2160\u2161 Roman",
        "\u0663 Arabic-Indic",
    )
    for name in names:
        (tmp_path / name).mkdir()

    result = sorted_scandir(str(tmp_path), str(tmp_path), sort=True)

    assert sorted(item.filename for item in result) == sorted(names)
    # the decimal run still sorts numerically while the superscript compares as text
    assert result.index(next(i for i in result if i.filename.endswith("2001"))) < result.index(
        next(i for i in result if i.filename.endswith("2002\u00b3"))
    )


def test_sorted_scandir_unsorted_by_default(tmp_path: Path) -> None:
    """Without the sort flag, entries are returned in raw scandir order."""
    for name in ("b.flac", "a.flac"):
        (tmp_path / name).touch()

    result = sorted_scandir(str(tmp_path), str(tmp_path))

    assert sorted(item.filename for item in result) == ["a.flac", "b.flac"]


def test_folder_signature_ignores_item_order() -> None:
    """The same set of files always produces the same signature."""
    items = [_file_item("pod/ep1.mp3"), _file_item("pod/ep2.mp3")]

    assert get_folder_signature(items) == get_folder_signature(list(reversed(items)))


def test_folder_signature_detects_changes() -> None:
    """Adding, removing, replacing or retagging a file changes the signature."""
    items = [_file_item("pod/ep1.mp3"), _file_item("pod/ep2.mp3")]
    signature = get_folder_signature(items)

    assert get_folder_signature([*items, _file_item("pod/ep3.mp3")]) != signature
    assert get_folder_signature(items[:1]) != signature
    # same number of files, but one of them replaced, retagged or renamed
    for changed in (
        _file_item("pod/ep2.mp3", checksum="1800000000"),
        _file_item("pod/ep2.mp3", file_size=2048),
        _file_item("pod/renamed.mp3"),
    ):
        assert get_folder_signature([items[0], changed]) != signature


def test_folder_signature_cannot_be_forged_by_a_filename() -> None:
    """A filename spelling out another entry does not collide with the entries it names."""
    real = [_file_item("a.mp3"), _file_item("b.mp3", checksum="1800000000", file_size=2048)]
    forged = [_file_item("a.mp3:1700000000:1024|b.mp3", checksum="1800000000", file_size=2048)]

    assert get_folder_signature(forged) != get_folder_signature(real)
