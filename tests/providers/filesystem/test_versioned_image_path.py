"""Tests for LocalFileSystemProvider cover image cache-busting."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.filesystem_local import LocalFileSystemProvider


def _create_provider() -> LocalFileSystemProvider:
    """Create a bare LocalFileSystemProvider without running __init__."""
    with patch.object(LocalFileSystemProvider, "__init__", lambda *_a, **_kw: None):
        provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.logger = MagicMock()
    return provider


class TestVersionedImagePath:
    """Test the cover image path versioning and its round-trip in resolve_image."""

    def test_appends_checksum(self) -> None:
        """A checksum is appended as a query suffix."""
        result = LocalFileSystemProvider._versioned_image_path("Moby Dick/folder.jpg", "123")
        assert result == "Moby Dick/folder.jpg?cs=123"

    def test_changes_when_file_replaced(self) -> None:
        """A new checksum (file replaced in place) yields a different path."""
        old = LocalFileSystemProvider._versioned_image_path("Moby Dick/folder.jpg", "123")
        new = LocalFileSystemProvider._versioned_image_path("Moby Dick/folder.jpg", "456")
        assert old != new

    def test_no_checksum_is_unchanged(self) -> None:
        """Without a checksum the path is returned untouched."""
        assert (
            LocalFileSystemProvider._versioned_image_path("Moby Dick/folder.jpg", None)
            == "Moby Dick/folder.jpg"
        )

    @pytest.mark.asyncio
    async def test_resolve_image_strips_suffix(self) -> None:
        """resolve_image strips the cache-busting suffix before resolving the file."""
        provider = _create_provider()
        versioned = LocalFileSystemProvider._versioned_image_path("Moby Dick/folder.jpg", "123")
        with patch.object(provider, "resolve", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = MagicMock(absolute_path="/music/folder.jpg")
            await provider.resolve_image(versioned)
        mock_resolve.assert_awaited_once_with("Moby Dick/folder.jpg")

    @pytest.mark.asyncio
    async def test_resolve_image_without_suffix(self) -> None:
        """A plain path without a suffix is resolved unchanged."""
        provider = _create_provider()
        with patch.object(provider, "resolve", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.return_value = MagicMock(absolute_path="/music/track.flac")
            await provider.resolve_image("Moby Dick/track.flac")
        mock_resolve.assert_awaited_once_with("Moby Dick/track.flac")

    @pytest.mark.asyncio
    async def test_resolve_image_missing_file_raises_media_not_found(self) -> None:
        """A removed image file surfaces as a typed MediaNotFoundError with chaining."""
        provider = _create_provider()
        with patch.object(provider, "resolve", new_callable=AsyncMock) as mock_resolve:
            mock_resolve.side_effect = FileNotFoundError
            with pytest.raises(MediaNotFoundError) as exc_info:
                await provider.resolve_image("Moby Dick/folder.jpg?cs=123")
        assert isinstance(exc_info.value.__cause__, FileNotFoundError)
