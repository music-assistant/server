"""Tests for sidecar collection during traversal, image versioning, NFO reads, and CUE reparse."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import Album, ProviderMapping, Track

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
)
from music_assistant.providers.filesystem_local.cue import make_cue_track_id
from music_assistant.providers.filesystem_local.helpers import (
    FileSystemItem,
    ScanErrors,
    SidecarIndex,
    SidecarReadError,
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


def _dir(relative_path: str) -> FileSystemItem:
    """Build a directory FileSystemItem."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=True,
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
    provider._sync_mapped_album_dirs = set()
    return provider


async def test_album_images_are_versioned_with_the_file_checksum() -> None:
    """Album folder images carry a ``?cs=`` suffix so replaced bytes bypass the image cache."""
    provider = _provider()
    provider._active_sidecar_index.record(_file("Artist/Album/folder.jpg", checksum="111"))
    images = await provider._get_local_images(
        "Artist/Album", extra_thumb_names=("album",), versioned=True
    )
    assert [img.path for img in images] == ["Artist/Album/folder.jpg?cs=111"]


async def test_malformed_album_nfo_is_ignored_without_raising() -> None:
    """A scalar-root album.nfo yields no snapshot and leaves the album untouched."""
    provider = _provider()
    provider._read_file = AsyncMock(return_value=b"<album>just text</album>")
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    snapshot = await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))
    assert snapshot is None
    assert album.name == "Keep"


async def test_transient_nfo_read_failure_raises_sidecar_read_error() -> None:
    """An IO/provider failure reading the NFO raises rather than looking like a removed NFO."""
    provider = _provider()
    provider._read_file = AsyncMock(side_effect=ProviderUnavailableError("network down"))
    album = Album(item_id="x", provider=INSTANCE_ID, name="Keep", provider_mappings=set())
    with pytest.raises(SidecarReadError):
        await provider._apply_album_nfo(album, _file("Artist/Album/album.nfo"))


async def test_local_walk_collects_sidecars_track_dirs_and_skips_strays() -> None:
    """The local walk records sidecars + track folders and drops stray images/nfo."""
    provider = _provider()
    provider.config.get_value = MagicMock(
        side_effect=lambda key: False if key == CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS.key else None
    )
    provider._classify_scan_item = MagicMock()
    index = SidecarIndex()
    walk_items = [
        _file("Artist/Album/track.mp3"),
        _file("Artist/Album/Disc 1/track.mp3"),
        _file("Artist/Album/album.nfo"),
        _file("Artist/Album/folder.jpg"),
        _file("Random/IMG_1234.jpg"),  # stray
        _file("Random/movie.nfo"),  # stray
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
    assert index.nfo_item("Artist/Album", "album.nfo") is not None
    assert index.track_dirs == {"Artist/Album", "Artist/Album/Disc 1"}
    assert index.files("Random") == []
    classified = [
        call.args[0].relative_path for call in provider._classify_scan_item.call_args_list
    ]
    assert classified == ["Artist/Album/track.mp3", "Artist/Album/Disc 1/track.mp3"]


async def test_virtual_walk_reuses_directory_listings_without_per_track_probes() -> None:
    """A WebDAV-style walk records sidecars + track folders from listings, one scan per dir."""
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
    assert scandir_calls == ["", "Artist", "Artist/Album"]
    assert index.nfo_item("Artist/Album", "album.nfo") is not None
    assert index.track_dirs == {"Artist/Album"}


async def test_reparse_album_from_cue_track_id() -> None:
    """A CUE-only album is reparsed via the CUE handler, not treated as unreadable."""
    provider = _provider()
    cue_id = make_cue_track_id("Artist/Album/album.cue", 1)
    track = Track(
        item_id="1",
        provider="library",
        name="t1",
        provider_mappings={
            ProviderMapping(
                item_id=cue_id, provider_domain="filesystem_local", provider_instance=INSTANCE_ID
            )
        },
    )
    provider.mass.music.albums.tracks = AsyncMock(return_value=[track])
    provider.resolve = AsyncMock(return_value=_file("Artist/Album/album.cue"))
    cue_album = Album(
        item_id="Artist/Album", provider=INSTANCE_ID, name="Cue Album", provider_mappings=set()
    )
    cue_track = MagicMock(spec=Track)
    cue_track.album = cue_album
    provider._cue = MagicMock()
    provider._cue.parse_tracks = AsyncMock(return_value=[cue_track])

    result = await provider._reparse_album_from_track("5")
    assert result is cue_album
    provider._cue.parse_tracks.assert_awaited_once()


async def test_music_sync_skips_sidecars_when_track_sync_disabled() -> None:
    """A playlist-only music sync must not build sidecar state or refresh albums/artists."""
    config_values = {
        CONF_ENTRY_CONTENT_TYPE.key: "music",
        CONF_ENTRY_LIBRARY_SYNC_TRACKS.key: False,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS.key: True,
    }
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider: Any = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock()
    provider.config.get_value = MagicMock(side_effect=lambda key: config_values.get(key))
    provider.media_content_type = "music"
    provider.sync_running = False
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.mass.music.database.get_rows_from_query = AsyncMock(return_value=[])
    provider._process_deletions = AsyncMock()
    provider._process_orphaned_albums_and_artists = AsyncMock()
    provider._set_available = MagicMock()
    provider._query_mapping_details = AsyncMock(return_value=({}, {}))
    provider._refresh_changed_sidecars = AsyncMock()

    captured: dict[str, Any] = {}

    async def _enumerate(**kwargs: Any) -> None:
        captured["sidecar_index"] = kwargs["sidecar_index"]

    provider._enumerate_files_for_sync = _enumerate
    await provider.sync_library(MediaType.TRACK)

    assert captured["sidecar_index"] is None
    provider._refresh_changed_sidecars.assert_not_awaited()
    provider._query_mapping_details.assert_not_awaited()
