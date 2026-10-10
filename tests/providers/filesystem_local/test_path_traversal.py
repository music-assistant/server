"""Tests that the filesystem provider rejects path-traversal outside its base path."""

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.filesystem_local import LocalFileSystemProvider, helpers
from music_assistant.providers.filesystem_local.helpers import FileSystemItem, ScanErrors

INSTANCE_ID = "filesystem_local--test"


def _make_provider(base_path: str) -> LocalFileSystemProvider:
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.base_path = base_path
    provider.logger = MagicMock()
    provider.write_access = False
    provider.media_content_type = "music"
    provider.config = MagicMock()
    provider.config.instance_id = INSTANCE_ID
    provider.manifest = MagicMock()
    provider.manifest.domain = "filesystem_local"
    return provider


@pytest.fixture
def music_tree(tmp_path: Path) -> Path:
    """Create a base dir with one in-base track and a secret file outside it."""
    base = tmp_path / "music"
    base.mkdir()
    (base / "track.mp3").write_bytes(b"\x00" * 128)
    (tmp_path / "secret.mp3").write_bytes(b"\x00" * 128)
    return base


def test_get_absolute_path_rejects_traversal(music_tree: Path) -> None:
    """A `../`-escaping relative path is rejected by the helper."""
    with pytest.raises(MediaNotFoundError):
        helpers.get_absolute_path(str(music_tree), "../secret.mp3")


def test_get_absolute_path_rejects_absolute_outside_base(music_tree: Path) -> None:
    """An absolute path outside the base is rejected by the helper."""
    outside = str(music_tree.parent / "secret.mp3")
    with pytest.raises(MediaNotFoundError):
        helpers.get_absolute_path(str(music_tree), outside)


def test_get_absolute_path_allows_in_base(music_tree: Path) -> None:
    """A legitimate in-base path still resolves."""
    result = helpers.get_absolute_path(str(music_tree), "track.mp3")
    assert result == str(music_tree / "track.mp3")


def test_get_absolute_path_allows_absolute_in_base(music_tree: Path) -> None:
    """An already-absolute in-base path still resolves (used by internal scans)."""
    abs_in_base = str(music_tree / "track.mp3")
    assert helpers.get_absolute_path(str(music_tree), abs_in_base) == abs_in_base


async def test_resolve_rejects_traversal(music_tree: Path) -> None:
    """provider.resolve() (used by get_track/preview) rejects `../` escapes."""
    provider = _make_provider(str(music_tree))
    with pytest.raises(MediaNotFoundError):
        await provider.resolve("../secret.mp3")


async def test_exists_rejects_traversal(music_tree: Path) -> None:
    """provider.exists() refuses a `../`-escaping path instead of answering for it."""
    provider = _make_provider(str(music_tree))
    with pytest.raises(MediaNotFoundError) as exc_info:
        await provider.exists("../secret.mp3")
    assert exc_info.value.translation_key == "path_outside_folder"


async def test_scandir_rejects_traversal(music_tree: Path) -> None:
    """provider._scandir() (used by browse) rejects listing a dir outside the base."""
    provider = _make_provider(str(music_tree))
    with pytest.raises(MediaNotFoundError):
        await provider._scandir("..")


async def test_resolve_allows_in_base(music_tree: Path) -> None:
    """A legitimate in-base file still resolves through provider.resolve()."""
    provider = _make_provider(str(music_tree))
    file_item = await provider.resolve("track.mp3")
    assert file_item.absolute_path == os.path.join(str(music_tree), "track.mp3")


def test_get_absolute_path_allows_symlink_inside_base(music_tree: Path) -> None:
    """The lexical helper keeps a symlink inside the base; reads check where it leads."""
    link = music_tree / "linked.mp3"
    link.symlink_to(music_tree.parent / "secret.mp3")
    assert helpers.get_absolute_path(str(music_tree), "linked.mp3") == str(link)


@pytest.fixture
def linked_tree(music_tree: Path) -> Path:
    """
    Add symlinks to the base dir: to a folder and a file outside it, and to a file inside it.

    The folder outside holds a track, a cover and a playlist; a third link leads nowhere.
    """
    target = music_tree.parent / "hidden_target"
    target.mkdir()
    (target / "song.mp3").write_bytes(b"\x00" * 128)
    (target / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (target / "list.m3u").write_text("#EXTM3U\n")
    (music_tree / "elsewhere").symlink_to(target, target_is_directory=True)
    (music_tree / "secret_link.mp3").symlink_to(music_tree.parent / "secret.mp3")
    (music_tree / "ghost").symlink_to(music_tree.parent / "missing", target_is_directory=True)
    (music_tree / "inner.mp3").symlink_to(music_tree / "track.mp3")
    return music_tree


_READS: dict[str, Callable[[LocalFileSystemProvider, str], Awaitable[object]]] = {
    "resolve": lambda provider, path: provider.resolve(path),
    "exists": lambda provider, path: provider.exists(path),
    "read_file": lambda provider, path: provider._read_file(path),
    "get_track": lambda provider, path: provider.get_track(path),
    "get_playlist_tracks": lambda provider, path: provider.get_playlist_tracks(path),
    "resolve_image": lambda provider, path: provider.resolve_image(path),
    "scandir": lambda provider, path: provider._scandir(path),
}


@pytest.mark.parametrize("read", list(_READS))
@pytest.mark.parametrize(
    "path",
    [
        "elsewhere/song.mp3",
        "elsewhere/cover.jpg",
        "elsewhere/list.m3u",
        "elsewhere",
        "secret_link.mp3",
        "ghost/song.mp3",
    ],
)
async def test_reads_refuse_a_symlink_leading_outside(
    linked_tree: Path, read: str, path: str
) -> None:
    """
    Every read refuses a path whose real location lies outside the base, also when it is missing.

    The refusal names the requested path only, never where the symlink leads.

    :param read: The read to perform.
    :param path: A path through a symlink that leads outside the base.
    """
    provider = _make_provider(str(linked_tree))

    with pytest.raises(MediaNotFoundError) as exc_info:
        await _READS[read](provider, path)

    assert exc_info.value.translation_key == "path_outside_folder"
    assert exc_info.value.translation_args == [str(linked_tree / path)]
    assert "hidden_target" not in str(exc_info.value)
    assert "missing" not in str(exc_info.value)


async def test_symlink_inside_base_is_read(linked_tree: Path) -> None:
    """A symlink whose real location stays inside the base is read through."""
    provider = _make_provider(str(linked_tree))

    file_item = await provider.resolve("inner.mp3")

    assert file_item.absolute_path == str(linked_tree / "inner.mp3")
    assert await provider._read_file("inner.mp3") == (linked_tree / "track.mp3").read_bytes()


async def test_optional_lookup_takes_a_refused_path_as_absent(linked_tree: Path) -> None:
    """An optional lookup, such as a root-level artist folder, skips a symlink leading outside."""
    provider = _make_provider(str(linked_tree))

    assert await provider._has_path("elsewhere") is False
    assert await provider._has_path("../secret.mp3") is False
    assert await provider._root_artist_path("elsewhere") is None
    assert await provider._has_path("track.mp3") is True


async def test_reads_refuse_a_base_symlink_re_pointed_after_first_use(tmp_path: Path) -> None:
    """A base that is a symlink keeps the real folder of its first use, also once re-pointed."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "track.mp3").write_bytes(b"\x00" * 128)
    other = tmp_path / "other"
    other.mkdir()
    (other / "secret.mp3").write_bytes(b"\x00" * 128)
    base = tmp_path / "base"
    base.symlink_to(allowed, target_is_directory=True)
    provider = _make_provider(str(base))
    assert await provider.exists("track.mp3")

    base.unlink()
    base.symlink_to(other, target_is_directory=True)

    with pytest.raises(MediaNotFoundError) as exc_info:
        await provider._read_file("secret.mp3")
    assert exc_info.value.translation_key == "path_outside_folder"
    scan_errors = ScanErrors()
    items_to_process: list[tuple[FileSystemItem, str | None]] = []
    await provider._enumerate_files_for_sync(
        file_checksums={},
        cue_file_checksums={},
        cur_filenames=set(),
        items_to_process=items_to_process,
        unchanged_cue_items=[],
        cue_stems=set(),
        scan_errors=scan_errors,
        metadata_files=[],
    )
    assert isinstance(scan_errors.fatal, MediaNotFoundError)
    assert not items_to_process
