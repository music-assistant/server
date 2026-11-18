"""Tests for the cover image a user created builtin playlist reports."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from music_assistant.providers.builtin import BuiltinProvider


def _make_provider(playlists_dir: Path) -> BuiltinProvider:
    """Create a minimal BuiltinProvider that reads playlists from the given directory."""
    prov = object.__new__(BuiltinProvider)
    prov.mass = MagicMock()
    prov.logger = MagicMock()
    prov.manifest = MagicMock(domain="builtin")
    prov.config = MagicMock(instance_id="builtin_1")
    prov._playlists_dir = str(playlists_dir)
    prov._playlist_lock = asyncio.Lock()
    return prov


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("https://example.com/cover.jpg", ["https://example.com/cover.jpg"]),
        ("/data/playlist_metadata_images/1_thumb.jpg", []),
    ],
    ids=["remote", "local"],
)
async def test_playlist_reports_only_a_remote_image(
    tmp_path: Path, image: str, expected: list[str]
) -> None:
    """A playlist only reports a remote cover image, not a local file written back to it."""
    (tmp_path / "my-playlist.m3u").write_text(
        f"#EXTM3U\n#PLAYLIST:My Playlist\n#EXTIMG:{image}\n", encoding="utf-8"
    )
    prov = _make_provider(tmp_path)

    playlist = await prov.get_playlist("my-playlist")

    assert [img.path for img in playlist.metadata.images or []] == expected
