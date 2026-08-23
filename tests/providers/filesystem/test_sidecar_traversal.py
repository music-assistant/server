"""Tests for sidecar collection during traversal and image path versioning."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.media_items import Album

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS
from music_assistant.providers.filesystem_local.helpers import (
    FileSystemItem,
    ScanErrors,
    SidecarIndex,
)
from music_assistant.providers.webdav.provider import WebDAVFileSystemProvider

INSTANCE_ID = "filesystem_local--test"


def _file(relative_path: str, checksum: str = "1") -> FileSystemItem:
    """Build a minimal file FileSystemItem."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=False,
        checksum=checksum,
        file_size=10,
    )


def _provider() -> Any:
    """Create a bare music provider with a working folder-images cache mock."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider.media_content_type = "music"
    provider.base_path = "/media"
    provider.cache = MagicMock()
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()
    provider._active_sidecar_index = SidecarIndex()
    return provider


async def test_album_images_are_versioned_with_the_file_checksum() -> None:
    """Album folder images carry a ``?cs=`` suffix so replaced bytes bypass the image cache."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/folder.jpg", checksum="111"))
    images = await provider._get_local_images(
        "Artist/Album", extra_thumb_names=("album",), versioned=True
    )
    assert [img.path for img in images] == ["Artist/Album/folder.jpg?cs=111"]


async def test_replacing_image_bytes_changes_the_versioned_path() -> None:
    """A new checksum for the same file yields a different versioned image path."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/folder.jpg", checksum="222"))
    images = await provider._get_local_images(
        "Artist/Album", extra_thumb_names=("album",), versioned=True
    )
    assert images[0].path == "Artist/Album/folder.jpg?cs=222"


async def test_malformed_album_nfo_is_ignored_without_raising() -> None:
    """A malformed/scalar-root album.nfo logs a warning and leaves the album untouched."""
    provider = _provider()
    provider._read_file = AsyncMock(return_value=b"<album>just text</album>")
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))
    assert album.name == "Keep"
    assert album.metadata.description is None
    assert provider.logger.warning.called


async def test_local_walk_collects_sidecars_and_skips_them_from_classification() -> None:
    """The local walk records NFO/image sidecars and only classifies importable files."""
    provider = _provider()
    provider.config.get_value = MagicMock(
        side_effect=lambda key: False if key == CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS.key else None
    )
    provider._classify_scan_item = MagicMock()
    index = SidecarIndex()
    walk_items = [
        _file("Artist/Album/track.mp3"),
        _file("Artist/Album/album.nfo"),
        _file("Artist/Album/folder.jpg"),
        _file("Artist/artist.nfo"),
    ]
    with patch(
        "music_assistant.providers.filesystem_local.recursive_iter", return_value=iter(walk_items)
    ):
        await provider._enumerate_files_for_sync(
            file_checksums={},
            cue_file_checksums={},
            cur_filenames=set(),
            items_to_process=[],
            unchanged_cue_items=[],
            cue_stems=set(),
            scan_errors=ScanErrors(),
            sidecar_index=index,
        )

    # sidecars captured, only the audio track classified
    assert index.nfo_item("Artist/Album", "album.nfo") is not None
    assert index.nfo_item("Artist", "artist.nfo") is not None
    assert [img.filename for img in index.image_items("Artist/Album")] == ["folder.jpg"]
    classified = [
        call.args[0].relative_path for call in provider._classify_scan_item.call_args_list
    ]
    assert classified == ["Artist/Album/track.mp3"]


def _dir(relative_path: str) -> FileSystemItem:
    """Build a directory FileSystemItem."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=True,
    )


async def test_virtual_walk_reuses_directory_listings_without_per_track_probes() -> None:
    """A WebDAV-style walk records sidecars from the listing it already fetches, one scan per dir."""
    with patch.object(WebDAVFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider: Any = WebDAVFileSystemProvider.__new__(WebDAVFileSystemProvider)
    provider.logger = MagicMock()
    provider.media_content_type = "music"
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(return_value=False)
    provider._classify_scan_item = MagicMock()

    listings = {
        "": [_dir("Artist")],
        "Artist": [_dir("Artist/Album")],
        "Artist/Album": [
            _file("Artist/Album/track.mp3"),
            _file("Artist/Album/album.nfo"),
            _file("Artist/Album/folder.jpg"),
        ],
    }
    scandir_calls: list[str] = []

    async def _scandir(path: str) -> list[FileSystemItem]:
        scandir_calls.append(path)
        return listings[path]

    provider._scandir = _scandir
    index = SidecarIndex()
    await provider._enumerate_files_for_sync(
        file_checksums={},
        cue_file_checksums={},
        cur_filenames=set(),
        items_to_process=[],
        unchanged_cue_items=[],
        cue_stems=set(),
        scan_errors=ScanErrors(),
        sidecar_index=index,
    )

    # exactly one listing per directory, no extra per-file/-track fetches
    assert scandir_calls == ["", "Artist", "Artist/Album"]
    assert index.nfo_item("Artist/Album", "album.nfo") is not None
    assert [img.filename for img in index.image_items("Artist/Album")] == ["folder.jpg"]
    classified = [
        call.args[0].relative_path for call in provider._classify_scan_item.call_args_list
    ]
    assert classified == ["Artist/Album/track.mp3"]
