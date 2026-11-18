"""Tests for filesystem playlist handling of mixed-case playlist extensions."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.filesystem_local import LocalFileSystemProvider

M3U_CONTENT = (
    "#EXTM3U\n"
    "#EXTINF:180,Artist - One\nArtist/Album/01 One.mp3\n"
    "#EXTINF:200,Artist - Two\nArtist/Album/02 Two.mp3\n"
    "#EXTINF:220,Artist - Three\nArtist/Album/03 Three.mp3\n"
)


def _create_provider(base_path: Path) -> LocalFileSystemProvider:
    """Create a music LocalFileSystemProvider rooted at base_path with mocked dependencies."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock(instance_id="filesystem_local--test")
    provider.media_content_type = "music"
    provider.base_path = str(base_path)
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.cache.get = AsyncMock(return_value=None)
    provider.mass.cache.set = AsyncMock()
    provider.exists = AsyncMock(return_value=True)  # type: ignore[method-assign]
    return provider


@pytest.mark.parametrize("filename", ["Mix.m3u", "Mix.M3U", "Mix.M3U8"])
async def test_get_playlist_tracks_parses_m3u_regardless_of_extension_case(
    tmp_path: Path, filename: str
) -> None:
    """An M3U playlist is parsed as M3U whatever the case of its extension."""
    provider = _create_provider(tmp_path)
    provider.resolve = AsyncMock(return_value=MagicMock(checksum="1"))  # type: ignore[method-assign]
    provider._read_file = AsyncMock(return_value=M3U_CONTENT.encode())  # type: ignore[method-assign]
    provider._parse_playlist_line = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda path, _parent: MagicMock(item_id=path)
    )

    tracks = await provider.get_playlist_tracks(f"Playlists/{filename}")

    assert [track.item_id for track in tracks] == [
        "Artist/Album/01 One.mp3",
        "Artist/Album/02 Two.mp3",
        "Artist/Album/03 Three.mp3",
    ]


@pytest.mark.parametrize("filename", ["Mix.m3u", "Mix.M3U", "Mix.M3U8"])
async def test_remove_playlist_tracks_parses_m3u_regardless_of_extension_case(
    tmp_path: Path, filename: str
) -> None:
    """Removing a track from an M3U playlist works whatever the case of its extension."""
    playlist_file = tmp_path / filename
    playlist_file.write_text(M3U_CONTENT, encoding="utf-8")
    provider = _create_provider(tmp_path)

    await provider.remove_playlist_tracks(filename, (2,))

    remaining = playlist_file.read_text(encoding="utf-8")
    assert "Artist/Album/01 One.mp3" in remaining
    assert "Artist/Album/02 Two.mp3" not in remaining
    assert "Artist/Album/03 Three.mp3" in remaining
