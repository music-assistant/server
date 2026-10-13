"""Tests that the filesystem provider writes playlists only to regular files inside its root."""

import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.playlists import parse_m3u
from music_assistant.providers.filesystem_local import LocalFileSystemProvider

ORIGINAL = "#EXTM3U\n#EXTINF:100,Original\nsome/track.mp3\n"


def _make_provider(base_path: Path) -> LocalFileSystemProvider:
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.base_path = str(base_path)
    provider.logger = MagicMock()
    provider.media_content_type = "music"
    provider.config = MagicMock(instance_id="filesystem_local--test")
    provider.get_playlist = AsyncMock()  # type: ignore[method-assign]
    provider.get_track = AsyncMock(return_value=_fake_track("Added"))  # type: ignore[method-assign]
    return provider


def _fake_track(name: str, duration: int = 200) -> MagicMock:
    track = MagicMock(duration=duration)
    track.name = name
    return track


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """Create the library root."""
    root = tmp_path / "music"
    root.mkdir()
    return root


async def test_add_playlist_tracks_refuses_a_symlinked_playlist(base: Path) -> None:
    """A playlist that is a symlink is not written through to its target."""
    target = base / "other.m3u"
    target.write_text(ORIGINAL, encoding="utf-8")
    (base / "evil.m3u").symlink_to(target)
    provider = _make_provider(base)

    with pytest.raises(InvalidDataError):
        await provider.add_playlist_tracks("evil.m3u", ["Artist/Song.mp3"])

    assert target.read_text(encoding="utf-8") == ORIGINAL


async def test_add_playlist_tracks_refuses_a_symlinked_parent(base: Path) -> None:
    """A playlist under a symlinked directory is not written through the link."""
    real_dir = base / "real"
    real_dir.mkdir()
    (real_dir / "party.m3u").write_text(ORIGINAL, encoding="utf-8")
    (base / "linked").symlink_to(real_dir, target_is_directory=True)
    provider = _make_provider(base)

    with pytest.raises(InvalidDataError):
        await provider.add_playlist_tracks("linked/party.m3u", ["Artist/Song.mp3"])

    assert (real_dir / "party.m3u").read_text(encoding="utf-8") == ORIGINAL


async def test_add_playlist_tracks_leaves_a_hard_linked_target_alone(
    base: Path, tmp_path: Path
) -> None:
    """Writing over a hard link replaces the directory entry and leaves the linked file as is."""
    outside = tmp_path / "outside.txt"
    outside.write_text(ORIGINAL, encoding="utf-8")
    os.link(outside, base / "linked.m3u")
    provider = _make_provider(base)

    await provider.add_playlist_tracks("linked.m3u", ["some/added.mp3"])

    assert outside.read_text(encoding="utf-8") == ORIGINAL
    assert "Added" in (base / "linked.m3u").read_text(encoding="utf-8")
    assert not [p for p in base.iterdir() if p.name.endswith(".tmp")]


async def test_add_playlist_tracks_keeps_the_playlist_permissions(base: Path) -> None:
    """Rewriting a playlist keeps the permission bits the file had."""
    playlist = base / "private.m3u"
    playlist.write_text(ORIGINAL)
    playlist.chmod(0o600)
    provider = _make_provider(base)

    await provider.add_playlist_tracks("private.m3u", ["some/added.mp3"])

    assert stat.S_IMODE(playlist.stat().st_mode) == 0o600
    assert "Added" in playlist.read_text()


@pytest.mark.skipif(os.geteuid() == 0, reason="root may write read-only files")
async def test_add_playlist_tracks_respects_a_read_only_playlist(base: Path) -> None:
    """A playlist the server may not write to is not replaced either."""
    playlist = base / "locked.m3u"
    playlist.write_text(ORIGINAL)
    playlist.chmod(0o444)
    provider = _make_provider(base)

    with pytest.raises(InvalidDataError):
        await provider.add_playlist_tracks("locked.m3u", ["some/added.mp3"])

    assert playlist.read_text() == ORIGINAL


async def test_add_playlist_tracks_keeps_entry_on_one_line(base: Path) -> None:
    """Line breaks in a track name or path cannot add lines to the playlist."""
    (base / "party.m3u").write_text("#EXTM3U\n", encoding="utf-8")
    provider = _make_provider(base)
    provider.get_track = AsyncMock(  # type: ignore[method-assign]
        return_value=_fake_track("Song\n#EXTINF:0,x\n/etc/passwd")
    )

    await provider.add_playlist_tracks("party.m3u", ["Artist/Song.mp3\n/etc/shadow"])

    lines = (base / "party.m3u").read_text(encoding="utf-8").splitlines()
    assert "/etc/passwd" not in lines
    assert "/etc/shadow" not in lines
    assert not any(line.startswith("#EXTINF:0,x") for line in lines)
    items = parse_m3u("\n".join(lines))
    assert len(items) == 1
    assert (items[0].title or "").startswith("Song")
    assert items[0].path.startswith("Artist/Song.mp3")
