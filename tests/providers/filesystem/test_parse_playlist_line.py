"""Tests for LocalFileSystemProvider._parse_playlist_line path resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.helpers.uri import create_uri
from music_assistant.providers.filesystem_local import LocalFileSystemProvider

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def music_dir(tmp_path: Path) -> Path:
    """Create a music folder with a playlist next to an artist folder."""
    (tmp_path / "Playlists").mkdir()
    (tmp_path / "Playlists" / "list.m3u").write_text("../Artist/track.mp3\n")
    (tmp_path / "Artist").mkdir()
    (tmp_path / "Artist" / "track.mp3").write_bytes(b"audio-bytes")
    return tmp_path


@pytest.fixture
def provider(
    music_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalFileSystemProvider, MagicMock]:
    """Create a provider on the music folder with tag parsing and track building stubbed."""
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.base_path = str(music_dir)
    provider.logger = MagicMock()
    provider.config = MagicMock(instance_id="filesystem_local--test")
    provider.mass = MagicMock()
    provider.mass.music.tracks.get_library_item_by_prov_id = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "music_assistant.providers.filesystem_local.async_parse_tags",
        AsyncMock(return_value=MagicMock()),
    )
    track = MagicMock(name="track")
    provider._parse_track = AsyncMock(return_value=track)  # type: ignore[method-assign]
    return provider, track


async def test_line_relative_to_playlist_folder(
    provider: tuple[LocalFileSystemProvider, MagicMock],
) -> None:
    """A ../ line resolves against the playlist's own folder."""
    fs_provider, track = provider

    result = await fs_provider._parse_playlist_line("../Artist/track.mp3", "Playlists")

    assert result is track
    file_item = fs_provider._parse_track.await_args.args[0]  # type: ignore[attr-defined]
    assert file_item.relative_path == "Artist/track.mp3"


async def test_line_relative_to_base_path(
    provider: tuple[LocalFileSystemProvider, MagicMock],
) -> None:
    """A base-path-relative line resolves after the playlist-folder candidate misses."""
    fs_provider, track = provider

    assert await fs_provider._parse_playlist_line("Artist/track.mp3", "Playlists") is track


async def test_absolute_line(
    provider: tuple[LocalFileSystemProvider, MagicMock], music_dir: Path
) -> None:
    """An absolute path in the playlist still resolves."""
    fs_provider, track = provider
    absolute_line = str(music_dir / "Artist" / "track.mp3")

    assert await fs_provider._parse_playlist_line(absolute_line, "Playlists") is track


async def test_file_uri_line(
    provider: tuple[LocalFileSystemProvider, MagicMock], music_dir: Path
) -> None:
    """A file:// uri with url-encoded characters resolves."""
    fs_provider, track = provider
    line = f"file://{music_dir}/Artist/track%2Emp3"

    assert await fs_provider._parse_playlist_line(line, "Playlists") is track


async def test_missing_file_returns_none(
    provider: tuple[LocalFileSystemProvider, MagicMock],
) -> None:
    """A line pointing at a nonexistent file logs a warning instead of raising."""
    fs_provider, _ = provider

    assert await fs_provider._parse_playlist_line("Missing/nope.mp3", "Playlists") is None
    fs_provider.logger.warning.assert_called_once()  # type: ignore[attr-defined]


async def test_library_track_preferred_over_tag_parsing(
    provider: tuple[LocalFileSystemProvider, MagicMock],
) -> None:
    """A line whose file is already in the library resolves from the db, without tag parsing."""
    fs_provider, _ = provider
    library_track = MagicMock(name="library_track")
    db_lookup = AsyncMock(return_value=library_track)
    fs_provider.mass.music.tracks.get_library_item_by_prov_id = db_lookup  # type: ignore[method-assign]

    result = await fs_provider._parse_playlist_line("../Artist/track.mp3", "Playlists")

    assert result is library_track
    db_lookup.assert_awaited_once_with("Artist/track.mp3", "filesystem_local--test")
    # the library track is presented under its provider item identity
    assert result.item_id == "Artist/track.mp3"
    assert result.provider == "filesystem_local--test"
    assert result.uri == create_uri(MediaType.TRACK, "filesystem_local--test", "Artist/track.mp3")
    fs_provider._parse_track.assert_not_awaited()  # type: ignore[attr-defined]


async def test_tag_parsing_fallback_when_not_in_library(
    provider: tuple[LocalFileSystemProvider, MagicMock],
) -> None:
    """A line whose file is not (yet) in the library falls back to parsing the file tags."""
    fs_provider, track = provider

    assert await fs_provider._parse_playlist_line("../Artist/track.mp3", "Playlists") is track
    fs_provider.mass.music.tracks.get_library_item_by_prov_id.assert_awaited_once()  # type: ignore[attr-defined]
    fs_provider._parse_track.assert_awaited_once()  # type: ignore[attr-defined]
