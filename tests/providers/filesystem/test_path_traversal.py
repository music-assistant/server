"""Tests that the filesystem provider rejects path-traversal outside its base path."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.filesystem_local import LocalFileSystemProvider, helpers

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
    """provider.exists() returns False for a `../`-escaping path instead of leaking it."""
    provider = _make_provider(str(music_tree))
    assert await provider.exists("../secret.mp3") is False


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
    """A symlink inside the base still resolves (check is lexical, symlinks are admin-created)."""
    link = music_tree / "linked.mp3"
    link.symlink_to(music_tree.parent / "secret.mp3")
    assert helpers.get_absolute_path(str(music_tree), "linked.mp3") == str(link)
