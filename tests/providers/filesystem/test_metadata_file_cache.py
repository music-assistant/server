"""Tests for the lightweight local metadata-file (NFO/image) change-detection cache."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.media_items import UniqueList

from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.filesystem_local.constants import CACHE_CATEGORY_METADATA_FILE
from music_assistant.providers.filesystem_local.helpers import FileSystemItem

INSTANCE_ID = "filesystem_local--test"


def _provider() -> Any:
    """Create a bare provider with a mocked cache."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.logger = MagicMock()
    provider.mass = MagicMock()
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider.media_content_type = "music"
    provider._sync_tracks = True
    provider.cache = MagicMock()
    return provider


def _item(relative_path: str, checksum: str = "1") -> FileSystemItem:
    """Build a minimal FileSystemItem, its checksum doubling as the metadata token."""
    return FileSystemItem(
        filename=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        absolute_path=f"/media/{relative_path}",
        is_dir=False,
        checksum=checksum,
    )


# --- _queue_changed_metadata_files (walk-time change detection) ------------


async def test_unchanged_metadata_file_is_ignored() -> None:
    """A metadata file whose token still matches its cache entry queues nothing."""
    provider = _provider()
    meta = _item("Artist/Album/album.nfo", checksum="1")
    provider.cache.get = AsyncMock(return_value={"token": "1", "track": "Artist/Album/t1.mp3"})
    items_to_process: list[tuple[FileSystemItem, str | None]] = []

    await provider._queue_changed_metadata_files([meta], {}, items_to_process)

    assert items_to_process == []


async def test_changed_nfo_queues_representative_track() -> None:
    """A changed NFO's registered representative track is queued for reparsing."""
    provider = _provider()
    meta = _item("Artist/Album/album.nfo", checksum="2")
    provider.cache.get = AsyncMock(return_value={"token": "1", "track": "Artist/Album/t1.mp3"})
    track_item = _item("Artist/Album/t1.mp3")
    provider.resolve = AsyncMock(return_value=track_item)
    items_to_process: list[tuple[FileSystemItem, str | None]] = []

    await provider._queue_changed_metadata_files(
        [meta], {"Artist/Album/t1.mp3": "abc"}, items_to_process
    )

    assert items_to_process == [(track_item, "abc")]


async def test_changed_image_queues_representative_track() -> None:
    """A changed recognized folder image queues its representative track too."""
    provider = _provider()
    meta = _item("Artist/Album/folder.jpg", checksum="2")
    provider.cache.get = AsyncMock(return_value={"token": "1", "track": "Artist/Album/t1.mp3"})
    track_item = _item("Artist/Album/t1.mp3")
    provider.resolve = AsyncMock(return_value=track_item)
    items_to_process: list[tuple[FileSystemItem, str | None]] = []

    await provider._queue_changed_metadata_files([meta], {}, items_to_process)

    assert items_to_process == [(track_item, None)]


async def test_two_changed_metadata_files_dedupe_to_one_track() -> None:
    """Two changed metadata files sharing a representative queue it only once."""
    provider = _provider()
    nfo = _item("Artist/Album/album.nfo", checksum="2")
    img = _item("Artist/Album/folder.jpg", checksum="9")
    cached = {
        "Artist/Album/album.nfo": {"token": "1", "track": "Artist/Album/t1.mp3"},
        "Artist/Album/folder.jpg": {"token": "8", "track": "Artist/Album/t1.mp3"},
    }
    provider.cache.get = AsyncMock(side_effect=lambda key, **_kwargs: cached[key])
    track_item = _item("Artist/Album/t1.mp3")
    provider.resolve = AsyncMock(return_value=track_item)
    items_to_process: list[tuple[FileSystemItem, str | None]] = []

    await provider._queue_changed_metadata_files([nfo, img], {}, items_to_process)

    assert len(items_to_process) == 1
    assert items_to_process[0][0] is track_item


async def test_track_already_changed_is_not_duplicated() -> None:
    """A representative already queued (its own content changed) is not queued twice."""
    provider = _provider()
    meta = _item("Artist/Album/album.nfo", checksum="2")
    provider.cache.get = AsyncMock(return_value={"token": "1", "track": "Artist/Album/t1.mp3"})
    provider.resolve = AsyncMock()
    existing = (_item("Artist/Album/t1.mp3"), "old")
    items_to_process: list[tuple[FileSystemItem, str | None]] = [existing]

    await provider._queue_changed_metadata_files([meta], {}, items_to_process)

    assert items_to_process == [existing]
    provider.resolve.assert_not_awaited()


async def test_cache_miss_is_ignored() -> None:
    """A metadata file with no cache entry (new/untracked) queues nothing."""
    provider = _provider()
    meta = _item("Artist/Album/album.nfo")
    provider.cache.get = AsyncMock(return_value=None)
    items_to_process: list[tuple[FileSystemItem, str | None]] = []

    await provider._queue_changed_metadata_files([meta], {}, items_to_process)

    assert items_to_process == []


async def test_missing_representative_track_defers() -> None:
    """A representative track that no longer resolves is skipped, not raised or written."""
    provider = _provider()
    meta = _item("Artist/Album/album.nfo", checksum="2")
    provider.cache.get = AsyncMock(return_value={"token": "1", "track": "Artist/Album/gone.mp3"})
    provider.resolve = AsyncMock(side_effect=FileNotFoundError())
    provider.cache.set = AsyncMock()
    items_to_process: list[tuple[FileSystemItem, str | None]] = []

    await provider._queue_changed_metadata_files([meta], {}, items_to_process)

    assert items_to_process == []
    provider.cache.set.assert_not_awaited()  # old token kept, so a later sync retries


# --- _classify_scan_item (walk routing, shared by local/WebDAV/cloud) ------


def test_classify_scan_item_routes_metadata_file_without_recording_it() -> None:
    """A recognized metadata file is collected separately and never treated as media."""
    provider = _provider()
    item = _item("Artist/Album/album.nfo")
    items_to_process: list[tuple[FileSystemItem, str | None]] = []
    cur_filenames: set[str] = set()
    metadata_files: list[FileSystemItem] = []

    provider._classify_scan_item(
        item,
        file_checksums={},
        cue_file_checksums={},
        cur_filenames=cur_filenames,
        items_to_process=items_to_process,
        unchanged_cue_items=[],
        cue_stems=set(),
        ignore_album_playlists=False,
        metadata_files=metadata_files,
    )

    assert metadata_files == [item]
    assert items_to_process == []
    assert cur_filenames == set()  # never present/absent-tracked, so never deleted either


def test_track_classification_uses_checksum_not_metadata_token() -> None:
    """A track's own change detection is driven only by checksum, imported-media compatible."""
    provider = _provider()
    item = FileSystemItem(
        filename="track.mp3",
        relative_path="Artist/Album/track.mp3",
        absolute_path="/media/Artist/Album/track.mp3",
        is_dir=False,
        checksum="100",
        metadata_token="999999999999",  # unrelated high-precision value, must not matter here
    )
    items_to_process: list[tuple[FileSystemItem, str | None]] = []
    cur_filenames: set[str] = set()

    provider._classify_scan_item(
        item,
        file_checksums={"Artist/Album/track.mp3": "100"},
        cue_file_checksums={},
        cur_filenames=cur_filenames,
        items_to_process=items_to_process,
        unchanged_cue_items=[],
        cue_stems=set(),
        ignore_album_playlists=False,
        metadata_files=[],
    )

    assert items_to_process == []
    assert "Artist/Album/track.mp3" in cur_filenames


# --- _register_metadata_file / _parse_artist integration -------------------


async def test_register_metadata_file_without_representative_is_a_no_op() -> None:
    """A metadata file read outside any track context (no representative) is never cached."""
    provider = _provider()
    provider.cache.set = AsyncMock()

    await provider._register_metadata_file(_item("Artist/artist.nfo"), None)

    provider.cache.set.assert_not_awaited()


async def test_parse_artist_registers_metadata_file_after_successful_read() -> None:
    """Reading artist.nfo during parsing registers its token and representative track."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.exists = AsyncMock(return_value=True)
    provider._read_file = AsyncMock(return_value=b"<artist><title>Name</title></artist>")
    provider._get_local_images = AsyncMock(return_value=UniqueList())
    provider.resolve = AsyncMock(return_value=_item("Artist/artist.nfo", checksum="42"))
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()

    await provider._parse_artist(
        "Name", artist_path="Artist", representative_track="Artist/Album/t1.mp3"
    )

    meta_calls = [
        call
        for call in provider.cache.set.await_args_list
        if call.kwargs.get("category") == CACHE_CATEGORY_METADATA_FILE
    ]
    assert len(meta_calls) == 1
    assert meta_calls[0].kwargs["key"] == "Artist/artist.nfo"
    assert meta_calls[0].kwargs["data"] == {"token": "42", "track": "Artist/Album/t1.mp3"}


async def test_parse_artist_does_not_register_when_nfo_read_fails() -> None:
    """A transient read failure while parsing artist.nfo never advances its cached token."""
    provider = _provider()
    provider.manifest = MagicMock(domain="filesystem_local")
    provider.exists = AsyncMock(return_value=True)
    provider._read_file = AsyncMock(side_effect=OSError("network blip"))
    provider.cache.get = AsyncMock(return_value=None)
    provider.cache.set = AsyncMock()

    with pytest.raises(OSError, match="network blip"):
        await provider._parse_artist(
            "Name", artist_path="Artist", representative_track="Artist/Album/t1.mp3"
        )

    provider.cache.set.assert_not_awaited()
