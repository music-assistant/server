"""Tests for the WebDAV provider directory scan logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.providers.filesystem_local.helpers import FileSystemItem, ScanErrors
from music_assistant.providers.webdav.helpers import WebDAVItem
from music_assistant.providers.webdav.provider import WebDAVFileSystemProvider

BASE_URL = "https://host.example/dav"


def _make_provider() -> WebDAVFileSystemProvider:
    provider = WebDAVFileSystemProvider.__new__(WebDAVFileSystemProvider)
    provider.base_url = BASE_URL
    provider.username = None
    provider.password = None
    provider.logger = MagicMock()
    provider.media_content_type = "music"
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(return_value=False)
    return provider


async def _run_enumerate(
    provider: WebDAVFileSystemProvider,
    *,
    file_checksums: dict[str, str] | None = None,
    cur_filenames: set[str] | None = None,
    scan_errors: ScanErrors | None = None,
) -> None:
    """Drive _enumerate_files_for_sync with empty sync buckets."""
    await provider._enumerate_files_for_sync(
        file_checksums=file_checksums or {},
        cue_file_checksums={},
        cur_filenames=cur_filenames if cur_filenames is not None else set(),
        items_to_process=[],
        unchanged_cue_items=[],
        cue_stems=set(),
        scan_errors=scan_errors if scan_errors is not None else ScanErrors(),
        metadata_files=[],
    )


def test_convert_skips_scanned_directory_with_special_chars() -> None:
    """
    The directory being scanned is returned by a depth-1 PROPFIND and must be skipped.

    A name with a URL-reserved character (here ``;``) previously slipped past the skip
    check, making the directory list itself and recurse until the recursion limit.
    """
    provider = _make_provider()
    scan_path = "Live; Unplugged"
    webdav_items = [
        WebDAVItem(href="/dav/Live; Unplugged", name="Live; Unplugged", is_dir=True),
        WebDAVItem(href="/dav/Live; Unplugged/01 track.mp3", name="01 track.mp3", is_dir=False),
    ]

    result = provider._convert_webdav_items(webdav_items, scan_path)

    relative_paths = [item.relative_path for item in result]
    assert relative_paths == ["Live; Unplugged/01 track.mp3"]
    assert scan_path not in relative_paths


def test_convert_handles_absolute_href_with_special_chars() -> None:
    """
    Some servers return absolute hrefs; the path must be extracted without a URL parser.

    ``urlparse`` would treat the ``;`` as params and truncate the name, mis-scanning the
    folder; the resolved relative path must keep reserved characters intact.
    """
    provider = _make_provider()
    scan_path = "Live; Unplugged"
    webdav_items = [
        WebDAVItem(
            href="https://host.example/dav/Live; Unplugged",
            name="Live; Unplugged",
            is_dir=True,
        ),
        WebDAVItem(
            href="https://host.example/dav/Live; Unplugged/01 track.mp3",
            name="01 track.mp3",
            is_dir=False,
        ),
    ]

    result = provider._convert_webdav_items(webdav_items, scan_path)

    assert [item.relative_path for item in result] == ["Live; Unplugged/01 track.mp3"]


def test_convert_skips_base_directory_at_root() -> None:
    """The base directory itself must be skipped when scanning the root."""
    provider = _make_provider()
    webdav_items = [
        WebDAVItem(href="/dav", name="dav", is_dir=True),
        WebDAVItem(href="/dav/Artist", name="Artist", is_dir=True),
    ]

    result = provider._convert_webdav_items(webdav_items, "")

    assert [item.relative_path for item in result] == ["Artist"]


async def test_enumerate_stops_on_directory_cycle() -> None:
    """A directory cycle must not exhaust the recursion limit."""
    provider = _make_provider()
    # A -> A/B -> A (back-edge to an ancestor); only the guard can break this
    listing = {
        "": [FileSystemItem("A", "A", "", is_dir=True)],
        "A": [FileSystemItem("B", "A/B", "", is_dir=True)],
        "A/B": [FileSystemItem("A", "A", "", is_dir=True)],
    }
    scanned: list[str] = []

    async def fake_scandir(path: str) -> list[FileSystemItem]:
        scanned.append(path)
        return listing[path]

    provider._scandir = AsyncMock(side_effect=fake_scandir)  # type: ignore[method-assign]

    await _run_enumerate(provider)

    # each directory is scanned exactly once despite the cycle
    assert sorted(scanned) == ["", "A", "A/B"]


async def test_enumerate_routes_metadata_files_separately_from_media() -> None:
    """The WebDAV walk collects NFO/images as metadata files, never as media or present files."""
    provider = _make_provider()
    provider._sync_tracks = True
    listing = {
        "": [
            FileSystemItem("Artist", "Artist", "", is_dir=True),
        ],
        "Artist": [
            FileSystemItem("track.mp3", "Artist/track.mp3", "", is_dir=False, checksum="1"),
            FileSystemItem("artist.nfo", "Artist/artist.nfo", "", is_dir=False, checksum="1"),
            FileSystemItem("folder.jpg", "Artist/folder.jpg", "", is_dir=False, checksum="1"),
            FileSystemItem("notes.txt", "Artist/notes.txt", "", is_dir=False, checksum="1"),
        ],
    }

    async def fake_scandir(path: str) -> list[FileSystemItem]:
        return listing[path]

    provider._scandir = AsyncMock(side_effect=fake_scandir)  # type: ignore[method-assign]
    items_to_process: list[tuple[FileSystemItem, str | None]] = []
    cur_filenames: set[str] = set()
    metadata_files: list[FileSystemItem] = []

    await provider._enumerate_files_for_sync(
        file_checksums={},
        cue_file_checksums={},
        cur_filenames=cur_filenames,
        items_to_process=items_to_process,
        unchanged_cue_items=[],
        cue_stems=set(),
        scan_errors=ScanErrors(),
        metadata_files=metadata_files,
    )

    assert [item.relative_path for item, _ in items_to_process] == ["Artist/track.mp3"]
    assert sorted(item.relative_path for item in metadata_files) == [
        "Artist/artist.nfo",
        "Artist/folder.jpg",
    ]
    # metadata files are never recorded present (unrelated to deletion tracking); notes.txt
    # is neither media nor a recognized metadata file, so the walk skips it entirely
    assert cur_filenames == set()


@pytest.mark.parametrize("special", ["Live; Unplugged", "Die drei ???", "Rock #1"])
async def test_enumerate_does_not_loop_on_special_chars(special: str) -> None:
    """Folders with reserved characters must be traversed without looping."""
    provider = _make_provider()
    track = FileSystemItem("t.mp3", f"{special}/t.mp3", "", is_dir=False, checksum="1")
    listing = {
        "": [FileSystemItem(special, special, "", is_dir=True)],
        special: [track],
    }

    async def fake_scandir(path: str) -> list[FileSystemItem]:
        return listing[path]

    provider._scandir = AsyncMock(side_effect=fake_scandir)  # type: ignore[method-assign]
    cur_filenames: set[str] = set()

    # the track is unchanged (checksum matches), so reaching it records it as present
    await _run_enumerate(
        provider, file_checksums={f"{special}/t.mp3": "1"}, cur_filenames=cur_filenames
    )

    assert cur_filenames == {f"{special}/t.mp3"}


async def test_is_reachable_asks_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WebDAV url cannot be stat'ed, so reachability is a live request to the server."""
    provider = _make_provider()
    provider.verify_ssl = True
    provider.mass = MagicMock()

    test_connection = AsyncMock()
    monkeypatch.setattr(
        "music_assistant.providers.webdav.provider.webdav_test_connection", test_connection
    )
    assert await provider._is_reachable() is True
    test_connection.assert_awaited_once()

    test_connection.side_effect = ProviderUnavailableError("server down")
    with pytest.raises(ProviderUnavailableError):
        await provider._is_reachable()
