"""Tests for image-cache invalidation when a local file changes during sync."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.filesystem_local import LocalFileSystemProvider


def _create_provider() -> tuple[LocalFileSystemProvider, AsyncMock]:
    """Create a bare LocalFileSystemProvider plus the invalidation mock it calls."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.logger = MagicMock()
    invalidate_mock = AsyncMock()
    provider.mass = MagicMock()
    provider.mass.metadata.invalidate_image_cache = invalidate_mock
    provider.media_content_type = "music"
    provider.config = MagicMock(instance_id="filesystem_local--test")
    return provider, invalidate_mock


def _file_item(relative_path: str) -> MagicMock:
    """Build a minimal FileSystemItem stand-in for _process_item_async."""
    item = MagicMock()
    item.relative_path = relative_path
    item.absolute_path = f"/music/{relative_path}"
    # unhandled extension: the sync branches are skipped, only the shared
    # invalidation logic at the top of _process_item_async runs
    item.ext = "unhandled"
    return item


async def test_changed_file_invalidates_both_image_path_forms() -> None:
    """A file with a previous checksum (= changed) busts both image path forms."""
    provider, invalidate_mock = _create_provider()
    item = _file_item("Artist/Album/track.mp3")
    await provider._process_item_async(item, prev_checksum="12345")
    assert invalidate_mock.await_count == 2
    invalidate_mock.assert_any_await("filesystem_local--test", "Artist/Album/track.mp3")
    invalidate_mock.assert_any_await("filesystem_local--test", "Artist/Album/track.mp3?cs=12345")


async def test_new_file_does_not_invalidate() -> None:
    """A file seen for the first time (no previous checksum) busts nothing."""
    provider, invalidate_mock = _create_provider()
    item = _file_item("Artist/Album/new-track.mp3")
    await provider._process_item_async(item, prev_checksum=None)
    invalidate_mock.assert_not_awaited()
