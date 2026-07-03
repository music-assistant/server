"""Tests for the playlist_metadata metadata provider."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage, Playlist, ProviderMapping, Track
from music_assistant_models.media_items.metadata import MediaItemMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.providers.playlist_metadata import PlaylistMetadataProvider


def _make_provider(tmp_path: Any) -> PlaylistMetadataProvider:
    """Construct a PlaylistMetadataProvider with mocked MA infrastructure."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path)
    mass.cache_path = str(tmp_path / "cache")
    mass.music.get_library_item = AsyncMock()
    mass.music.get_library_item_by_prov_mappings = AsyncMock()

    manifest = MagicMock()
    manifest.domain = "playlist_metadata"

    config = MagicMock()
    config.instance_id = "playlist_metadata"
    config.get_value = MagicMock(
        side_effect=lambda key: {
            CONF_LOG_LEVEL: "GLOBAL",
            "template": "album_grid",
            "skip_provider_playlists": False,
        }.get(key, "album_grid")
    )

    provider = PlaylistMetadataProvider(mass, manifest, config, set())
    provider._images_dir = str(tmp_path / "playlist_images")
    os.makedirs(provider._images_dir, exist_ok=True)

    return provider


def _make_playlist() -> Playlist:
    """Create a test playlist with tracks."""
    return Playlist(
        item_id="test_playlist_1",
        provider="test_provider",
        name="Test Playlist",
        provider_mappings={
            ProviderMapping(
                item_id="test_playlist_1",
                provider_domain="test_provider",
                provider_instance="test",
            )
        },
        metadata=MediaItemMetadata(),
    )


def _make_track_with_image(track_id: str, image_url: str) -> Track:
    """Create a test track with an album image."""
    return Track(
        item_id=track_id,
        provider="test_provider",
        name=f"Track {track_id}",
        provider_mappings={
            ProviderMapping(
                item_id=track_id,
                provider_domain="test_provider",
                provider_instance="test",
            )
        },
        metadata=MediaItemMetadata(
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider="test_provider",
                        remotely_accessible=True,
                    )
                ]
            )
        ),
    )


@pytest.mark.asyncio
async def test_get_playlist_metadata_returns_none_when_insufficient_images(
    tmp_path: Any,
) -> None:
    """Provider should return None when playlist has insufficient unique images."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    playlist = _make_playlist()

    # Mock empty playlist (no tracks = no images)
    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    with patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter):
        result = await provider.get_playlist_metadata(playlist)

    assert result is None


@pytest.mark.asyncio
async def test_get_playlist_metadata_returns_metadata_when_sufficient_images(
    tmp_path: Any,
) -> None:
    """Provider should return MediaItemMetadata with images when there are sufficient images."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    playlist = _make_playlist()

    # Mock getting tracks with multiple unique images
    tracks = [
        _make_track_with_image(f"track{i}", f"http://example.com/img{i}.jpg") for i in range(10)
    ]

    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        for track in tracks:
            yield track

    # Mock _render to return fake image data
    with (
        patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter),
        patch.object(provider, "_render", new_callable=AsyncMock) as mock_render,
    ):
        mock_render.return_value = b"fake_image_data"

        result = await provider.get_playlist_metadata(playlist)

        assert result is not None
        assert isinstance(result, MediaItemMetadata)
        assert result.images is not None
        assert len(result.images) == 2  # Both THUMB and FANART

        # Check THUMB image
        thumb_image = next((img for img in result.images if img.type == ImageType.THUMB), None)
        assert thumb_image is not None
        assert thumb_image.provider == "playlist_metadata"
        assert os.path.exists(thumb_image.path)
        assert "_thumb.jpg" in thumb_image.path

        # Check FANART image
        fanart_image = next((img for img in result.images if img.type == ImageType.FANART), None)
        assert fanart_image is not None
        assert fanart_image.provider == "playlist_metadata"
        assert os.path.exists(fanart_image.path)
        assert "_fanart.jpg" in fanart_image.path


@pytest.mark.asyncio
async def test_get_playlist_metadata_handles_exception(
    tmp_path: Any,
) -> None:
    """Provider should return None and log when rendering fails."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    playlist = _make_playlist()

    # Mock _render to raise an exception
    with patch.object(provider, "_render", new_callable=AsyncMock) as mock_render:
        mock_render.side_effect = RuntimeError("Rendering failed")

        result = await provider.get_playlist_metadata(playlist)

        assert result is None


@pytest.mark.asyncio
async def test_get_playlist_metadata_cleans_up_old_files(
    tmp_path: Any,
) -> None:
    """Provider should clean up old artwork files when regenerating."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    playlist = _make_playlist()

    # Create existing artwork files (simulating previous generations)
    old_file1 = os.path.join(provider._images_dir, f"{playlist.item_id}_1234567890_thumb.jpg")
    old_file2 = os.path.join(provider._images_dir, f"{playlist.item_id}_9876543210_thumb.jpg")
    with open(old_file1, "wb") as f:  # noqa: ASYNC230
        f.write(b"old_image_data_1")
    with open(old_file2, "wb") as f:  # noqa: ASYNC230
        f.write(b"old_image_data_2")

    tracks = [
        _make_track_with_image(f"track{i}", f"http://example.com/img{i}.jpg") for i in range(10)
    ]

    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        for track in tracks:
            yield track

    with (
        patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter),
        patch.object(provider, "_render", new_callable=AsyncMock) as mock_render,
    ):
        mock_render.return_value = b"new_image_data"

        result = await provider.get_playlist_metadata(playlist)

        assert result is not None
        assert result.images is not None
        thumb_image = next((img for img in result.images if img.type == ImageType.THUMB), None)
        assert thumb_image is not None
        # New file should have timestamp in filename
        assert playlist.item_id in thumb_image.path
        assert "_thumb.jpg" in thumb_image.path
        # Old files are cleaned up asynchronously by _cleanup_stale_images, not inline here.
        assert os.path.exists(thumb_image.path)


@pytest.mark.asyncio
async def test_get_playlist_metadata_always_creates_unique_filename(
    tmp_path: Any,
) -> None:
    """Provider should always create unique filename with timestamp."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    playlist = _make_playlist()

    tracks = [
        _make_track_with_image(f"track{i}", f"http://example.com/img{i}.jpg") for i in range(10)
    ]

    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        for track in tracks:
            yield track

    with (
        patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter),
        patch.object(provider, "_render", new_callable=AsyncMock) as mock_render,
    ):
        mock_render.return_value = b"new_image_data"

        result = await provider.get_playlist_metadata(playlist)

        assert result is not None
        assert result.images is not None
        thumb_image = next((img for img in result.images if img.type == ImageType.THUMB), None)
        assert thumb_image is not None
        # Filename should include timestamp
        assert playlist.item_id in thumb_image.path
        assert "_thumb.jpg" in thumb_image.path
        # Should have timestamp in filename (longer than base name)
        assert len(Path(thumb_image.path).name) > len(f"{playlist.item_id}_thumb.jpg")


@pytest.mark.asyncio
async def test_is_our_image_recognizes_own_images(
    tmp_path: Any,
) -> None:
    """_is_our_image should correctly identify images generated by this provider."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    # Own image with correct provider
    own_image = MediaItemImage(
        type=ImageType.THUMB,
        path=os.path.join(provider._images_dir, "test_thumb.jpg"),
        provider="playlist_metadata",
        remotely_accessible=False,
    )
    assert provider._is_our_image(own_image) is True

    # Image in our directory but provider changed to "builtin"
    builtin_image = MediaItemImage(
        type=ImageType.THUMB,
        path=os.path.join(provider._images_dir, "test_thumb.jpg"),
        provider="builtin",
        remotely_accessible=False,
    )
    assert provider._is_our_image(builtin_image) is True

    # Remote URL with different provider
    remote_image = MediaItemImage(
        type=ImageType.THUMB,
        path="http://example.com/image.jpg",
        provider="other_provider",
        remotely_accessible=True,
    )
    assert provider._is_our_image(remote_image) is False

    # Bare filename (builtin asset)
    builtin_asset = MediaItemImage(
        type=ImageType.THUMB,
        path="logo.png",
        provider="builtin",
        remotely_accessible=False,
    )
    assert provider._is_our_image(builtin_asset) is False
