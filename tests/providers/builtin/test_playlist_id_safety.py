"""Tests that a builtin playlist id can only ever name a file inside the playlists folder."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.builtin import BuiltinProvider


def _make_provider(playlists_dir: Path) -> BuiltinProvider:
    """Create a minimal BuiltinProvider that reads and writes M3U files under playlists_dir."""
    prov = object.__new__(BuiltinProvider)
    prov.mass = MagicMock()
    prov.logger = MagicMock()
    prov.manifest = MagicMock(domain="builtin")
    prov.config = MagicMock(instance_id="builtin")
    prov._playlists_dir = str(playlists_dir)
    prov._playlist_lock = asyncio.Lock()
    prov._playlist_locks = {}
    return prov


@pytest.mark.parametrize(
    "playlist_id", ["../outside", "./Mine", "sub/Mine", "/etc/passwd", "evil\x00", "", ".", ".."]
)
def test_playlist_file_rejects_ids_that_reach_outside_the_folder(
    tmp_path: Path, playlist_id: str
) -> None:
    """An id with a path component or a null byte never becomes a file path."""
    prov = _make_provider(tmp_path)
    with pytest.raises(MediaNotFoundError):
        prov._playlist_file(playlist_id)


@pytest.mark.parametrize("playlist_id", ["Mine", "My Mix (2)", "a..b"])
def test_playlist_file_accepts_a_plain_file_name(tmp_path: Path, playlist_id: str) -> None:
    """A plain file name, dots and spaces included, stays inside the folder."""
    prov = _make_provider(tmp_path)
    assert prov._playlist_file(playlist_id) == str(tmp_path / f"{playlist_id}.m3u")


@pytest.mark.asyncio
async def test_get_playlist_hides_a_playlist_reached_through_an_alias(tmp_path: Path) -> None:
    """The same file is served under its real id but not under a path that aliases it."""
    (tmp_path / "Secret.m3u").write_text("#EXTM3U\n#PLAYLIST:Secret\n", encoding="utf-8")
    prov = _make_provider(tmp_path)

    assert (await prov.get_playlist("Secret")).name == "Secret"
    with pytest.raises(MediaNotFoundError):
        await prov.get_playlist("./Secret")


@pytest.mark.asyncio
async def test_library_remove_never_deletes_a_file_outside_the_folder(tmp_path: Path) -> None:
    """A traversal id can not delete an M3U file elsewhere on disk."""
    outside = tmp_path.parent / "outside.m3u"
    outside.write_text("#EXTM3U\n", encoding="utf-8")
    playlists_dir = tmp_path / "playlists"
    playlists_dir.mkdir()
    prov = _make_provider(playlists_dir)

    with pytest.raises(MediaNotFoundError):
        await prov.library_remove("../outside", MediaType.PLAYLIST)
    assert outside.exists()
