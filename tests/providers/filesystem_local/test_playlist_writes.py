"""Tests that the filesystem provider writes playlists only to regular files inside its root."""

import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

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


@pytest.mark.parametrize("edit", ["add", "remove"])
async def test_editing_a_non_playlist_file_is_refused(base: Path, edit: str) -> None:
    """A mapped path without a playlist extension is never read or rewritten."""
    (base / "pkg").mkdir()
    module = base / "pkg" / "__init__.py"
    module.write_text("x = 1\n", encoding="utf-8")
    provider = _make_provider(base)

    call = (
        provider.add_playlist_tracks("pkg/__init__.py", ["evil.wav"])
        if edit == "add"
        else provider.remove_playlist_tracks("pkg/__init__.py", (1,))
    )

    with pytest.raises(MediaNotFoundError):
        await call

    assert module.read_text(encoding="utf-8") == "x = 1\n"


async def test_get_playlist_refuses_a_non_playlist_file(base: Path) -> None:
    """A path without a playlist extension does not resolve as a playlist."""
    (base / "notes.txt").write_text(ORIGINAL, encoding="utf-8")
    provider = _make_provider(base)
    del provider.get_playlist  # the real method, not the helper's stub

    with pytest.raises(MediaNotFoundError):
        await provider.get_playlist("notes.txt")


@pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
async def test_a_track_title_with_a_line_break_writes_one_entry(
    base: Path, line_break: str
) -> None:
    """
    A track title carrying a line break adds exactly one entry of two lines.

    :param line_break: The line break embedded in the track title.
    """
    (base / "party.m3u").write_text("#EXTM3U\n", encoding="utf-8")
    provider = _make_provider(base)
    provider.get_track = AsyncMock(  # type: ignore[method-assign]
        return_value=_fake_track(f"marker{line_break}inert_line = 1")
    )

    await provider.add_playlist_tracks("party.m3u", ["Artist/Song.mp3"])

    lines = [line for line in (base / "party.m3u").read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 3
    assert lines[0] == "#EXTM3U"
    assert lines[1].startswith("#EXTINF:")
    assert "marker" in lines[1]
    assert lines[2] == "Artist/Song.mp3"
    assert "inert_line = 1" not in lines


def _write_playlist(base: Path, name: str, paths: list[str]) -> Path:
    playlist = base / name
    playlist.write_text(
        "#EXTM3U\n" + "".join(f"\n#EXTINF:100,T\n{path}\n" for path in paths), encoding="utf-8"
    )
    return playlist


def _paths(playlist: Path) -> list[str]:
    return [item.path for item in parse_m3u(playlist.read_text(encoding="utf-8"))]


async def test_move_playlist_track_down_and_up(base: Path) -> None:
    """A single track moves to a later and then to an earlier position."""
    playlist = _write_playlist(base, "party.m3u", ["a.mp3", "b.mp3", "c.mp3", "d.mp3"])
    provider = _make_provider(base)

    await provider.move_playlist_track("party.m3u", 1, 3)
    assert _paths(playlist) == ["b.mp3", "c.mp3", "a.mp3", "d.mp3"]

    await provider.move_playlist_track("party.m3u", 4, 2)
    assert _paths(playlist) == ["b.mp3", "d.mp3", "c.mp3", "a.mp3"]


async def test_move_playlist_tracks_keeps_block_order(base: Path) -> None:
    """Non-adjacent tracks move as one block and keep their order."""
    playlist = _write_playlist(base, "party.m3u", ["a.mp3", "b.mp3", "c.mp3", "d.mp3", "e.mp3"])
    provider = _make_provider(base)

    await provider.move_playlist_tracks("party.m3u", [4, 1], 2)

    assert _paths(playlist) == ["b.mp3", "a.mp3", "d.mp3", "c.mp3", "e.mp3"]


async def test_move_playlist_tracks_to_the_end(base: Path) -> None:
    """Without a target position the tracks go to the end of the playlist."""
    playlist = _write_playlist(base, "party.m3u", ["a.mp3", "b.mp3", "c.mp3", "d.mp3"])
    provider = _make_provider(base)

    await provider.move_playlist_tracks("party.m3u", [1, 2])

    assert _paths(playlist) == ["c.mp3", "d.mp3", "a.mp3", "b.mp3"]


async def test_move_playlist_track_counts_entries_that_do_not_exist(base: Path) -> None:
    """An entry whose file is missing keeps its slot, so positions match the listing."""
    playlist = _write_playlist(base, "party.m3u", ["a.mp3", "gone.mp3", "c.mp3"])
    provider = _make_provider(base)

    await provider.move_playlist_track("party.m3u", 3, 1)

    assert _paths(playlist) == ["c.mp3", "a.mp3", "gone.mp3"]


async def test_move_playlist_track_in_a_file_with_a_byte_order_mark(base: Path) -> None:
    """A leading byte order mark does not shift the positions."""
    playlist = _write_playlist(base, "party.m3u8", ["a.mp3", "b.mp3", "c.mp3"])
    playlist.write_bytes(b"\xef\xbb\xbf" + playlist.read_bytes())
    provider = _make_provider(base)

    await provider.move_playlist_track("party.m3u8", 2, 1)

    assert _paths(playlist) == ["b.mp3", "a.mp3", "c.mp3"]


async def test_move_playlist_track_out_of_range_leaves_the_file_alone(base: Path) -> None:
    """A position outside the playlist is refused and nothing is written."""
    playlist = _write_playlist(base, "party.m3u", ["a.mp3", "b.mp3"])
    before = playlist.read_bytes()
    provider = _make_provider(base)

    with pytest.raises(InvalidDataError):
        await provider.move_playlist_track("party.m3u", 3, 1)

    assert playlist.read_bytes() == before


@pytest.mark.parametrize("filename", ["party.pls", "party.PLS"])
async def test_move_playlist_track_refuses_a_pls_playlist(base: Path, filename: str) -> None:
    """A .pls playlist is not reordered and stays as it was."""
    playlist = base / filename
    playlist.write_text("[playlist]\nFile1=a.mp3\nFile2=b.mp3\nNumberOfEntries=2\n")
    before = playlist.read_bytes()
    provider = _make_provider(base)

    with pytest.raises(InvalidDataError):
        await provider.move_playlist_track(filename, 1, 2)

    assert playlist.read_bytes() == before


@pytest.mark.parametrize("write_access", [True, False])
def test_move_feature_needs_write_access(base: Path, write_access: bool) -> None:
    """Reordering is only offered when the source can be written to."""
    provider = _make_provider(base)
    provider.write_access = write_access

    assert (ProviderFeature.PLAYLIST_TRACKS_MOVE in provider.supported_features) is write_access
