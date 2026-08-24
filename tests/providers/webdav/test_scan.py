"""Tests for the WebDAV provider directory scan logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import ProviderUnavailableError

from music_assistant.providers.filesystem_local.helpers import (
    FileSystemItem,
    ScanErrors,
    get_folder_signature,
)
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


def test_convert_keeps_last_modified_checksum_and_carries_etag_separately() -> None:
    """The HTTP date stays the checksum (compat); the ETag is carried as the sidecar token."""
    provider = _make_provider()
    webdav_items = [
        WebDAVItem(
            href="/dav/Artist/a.mp3",
            name="a.mp3",
            is_dir=False,
            size=10,
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
            etag="abc123",
        ),
        WebDAVItem(
            href="/dav/Artist/b.mp3",
            name="b.mp3",
            is_dir=False,
            size=10,
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
            etag=None,
        ),
    ]

    result = provider._convert_webdav_items(webdav_items, "")

    # the checksum is unaffected by the ETag, so an already-imported file's checksum survives
    # an upgrade unchanged and is never treated as "changed" just because ETags are now read
    assert result[0].checksum == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert result[1].checksum == "Wed, 01 Jan 2025 00:00:00 GMT"
    # the ETag (when present) is carried separately and sharpens sidecar-signature precision
    assert result[0].sidecar_token == "abc123"
    assert result[1].sidecar_token is None


def test_changed_etag_changes_sidecar_signature_same_date_and_size() -> None:
    """A replaced sidecar with the same HTTP date and size still changes the signature via ETag."""
    provider = _make_provider()

    def _folder_jpg(etag: str) -> WebDAVItem:
        return WebDAVItem(
            href="/dav/Artist/Album/folder.jpg",
            name="folder.jpg",
            is_dir=False,
            size=2048,
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
            etag=etag,
        )

    before = provider._convert_webdav_items([_folder_jpg("etag-1")], "Artist/Album")
    after = provider._convert_webdav_items([_folder_jpg("etag-2")], "Artist/Album")
    assert get_folder_signature(before) != get_folder_signature(after)
    # the versioned image URL also differs, so clients refetch the new bytes
    assert before[0].change_token != after[0].change_token


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
