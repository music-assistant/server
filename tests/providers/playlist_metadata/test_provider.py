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
    Path(provider._images_dir).mkdir(parents=True, exist_ok=True)

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


def _make_track_with_genres(track_id: str, genres: set[str]) -> Track:
    """Create a test track with genres."""
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
        metadata=MediaItemMetadata(genres=genres),
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
        assert Path(thumb_image.path).exists()
        assert "_thumb.jpg" in thumb_image.path

        # Check FANART image
        fanart_image = next((img for img in result.images if img.type == ImageType.FANART), None)
        assert fanart_image is not None
        assert fanart_image.provider == "playlist_metadata"
        assert Path(fanart_image.path).exists()
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
        assert Path(thumb_image.path).exists()


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


@pytest.mark.asyncio
async def test_analyze_playlist_genres_returns_most_common_genres(
    tmp_path: Any,
) -> None:
    """Provider should return the most common genres from playlist tracks."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    # Override config to enable genre detection
    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key: {
            CONF_LOG_LEVEL: "GLOBAL",
            "template": "album_grid",
            "skip_provider_playlists": False,
            "enable_genre_detection": True,
            "genre_min_threshold": 10,
            "genre_max_count": 3,
        }.get(key)
    )

    playlist = _make_playlist()

    # Create tracks with genres:
    # Rock: 15/26 = 57.7%
    # Pop: 5/26 = 19.2%
    # Jazz: 3/26 = 11.5% (above threshold)
    # Electronic: 2/26 = 7.7% (below 10% threshold)
    # Classical: 1/26 = 3.8% (below threshold)
    tracks = (
        [_make_track_with_genres(f"track{i}", {"Rock"}) for i in range(15)]
        + [_make_track_with_genres(f"track{i}", {"Pop"}) for i in range(15, 20)]
        + [_make_track_with_genres(f"track{i}", {"Jazz"}) for i in range(20, 23)]
        + [_make_track_with_genres(f"track{i}", {"Electronic"}) for i in range(23, 25)]
        + [_make_track_with_genres(f"track{i}", {"Classical"}) for i in range(25, 26)]
    )

    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        for track in tracks:
            yield track

    with patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter):
        result = await provider._analyze_playlist_genres(playlist)

    assert result is not None
    # Should return top 3 genres above 10% threshold
    assert result == {"Rock", "Pop", "Jazz"}


@pytest.mark.asyncio
async def test_analyze_playlist_genres_respects_threshold(
    tmp_path: Any,
) -> None:
    """Provider should filter out genres below the minimum threshold."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    # Override config with higher threshold
    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key: {
            CONF_LOG_LEVEL: "GLOBAL",
            "enable_genre_detection": True,
            "genre_min_threshold": 30,  # 30% threshold
            "genre_max_count": 5,
        }.get(key)
    )

    playlist = _make_playlist()

    # Rock: 10/20 = 50% (above threshold)
    # Pop: 5/20 = 25% (below threshold)
    tracks = (
        [_make_track_with_genres(f"track{i}", {"Rock"}) for i in range(10)]
        + [_make_track_with_genres(f"track{i}", {"Pop"}) for i in range(10, 15)]
        + [_make_track_with_genres(f"track{i}", set()) for i in range(15, 20)]
    )

    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        for track in tracks:
            yield track

    with patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter):
        result = await provider._analyze_playlist_genres(playlist)

    assert result is not None
    assert result == {"Rock"}  # Only Rock meets the 30% threshold


@pytest.mark.asyncio
async def test_analyze_playlist_genres_returns_none_for_empty_playlist(
    tmp_path: Any,
) -> None:
    """Provider should return None when playlist has no tracks."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key: {
            "enable_genre_detection": True,
            "genre_min_threshold": 10,
            "genre_max_count": 3,
        }.get(key)
    )

    playlist = _make_playlist()

    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    with patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter):
        result = await provider._analyze_playlist_genres(playlist)

    assert result is None


@pytest.mark.asyncio
async def test_analyze_playlist_genres_returns_none_when_no_genres_meet_threshold(
    tmp_path: Any,
) -> None:
    """Provider should return None when no genres meet the minimum threshold."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key: {
            "enable_genre_detection": True,
            "genre_min_threshold": 50,  # 50% threshold
            "genre_max_count": 3,
        }.get(key)
    )

    playlist = _make_playlist()

    # All genres below 50%
    tracks = (
        [_make_track_with_genres(f"track{i}", {"Rock"}) for i in range(4)]
        + [_make_track_with_genres(f"track{i}", {"Pop"}) for i in range(4, 7)]
        + [_make_track_with_genres(f"track{i}", set()) for i in range(7, 10)]
    )

    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        for track in tracks:
            yield track

    with patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter):
        result = await provider._analyze_playlist_genres(playlist)

    assert result is None


@pytest.mark.asyncio
async def test_analyze_playlist_genres_handles_exception(
    tmp_path: Any,
) -> None:
    """Provider should handle exceptions gracefully and return None."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key: {
            "enable_genre_detection": True,
            "genre_min_threshold": 10,
            "genre_max_count": 3,
        }.get(key)
    )

    playlist = _make_playlist()

    # Mock tracks to raise exception
    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]
        raise AttributeError("Failed to get tracks")

    with patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter):
        result = await provider._analyze_playlist_genres(playlist)

    assert result is None


@pytest.mark.asyncio
async def test_get_playlist_metadata_includes_genres_when_enabled(
    tmp_path: Any,
) -> None:
    """Provider should include genres in metadata when genre detection is enabled."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key: {
            CONF_LOG_LEVEL: "GLOBAL",
            "template": "album_grid",
            "skip_provider_playlists": False,
            "enable_genre_detection": True,
            "genre_min_threshold": 10,
            "genre_max_count": 3,
        }.get(key)
    )

    playlist = _make_playlist()

    tracks_with_images = [
        _make_track_with_image(f"track{i}", f"http://example.com/img{i}.jpg") for i in range(10)
    ]

    # Also add genres to these tracks
    for i, track in enumerate(tracks_with_images):
        if i < 5:
            track.metadata.genres = {"Rock"}
        else:
            track.metadata.genres = {"Pop"}

    async def mock_tracks_iter(
        _item_id: str,
        _provider: str,
        _force_refresh: bool = False,
        _allow_dynamic_tracks: bool = False,
    ) -> AsyncGenerator[Track]:
        for track in tracks_with_images:
            yield track

    with (
        patch.object(provider.mass.music.playlists, "tracks", side_effect=mock_tracks_iter),
        patch.object(provider, "_render", new_callable=AsyncMock) as mock_render,
    ):
        mock_render.return_value = b"fake_image_data"

        result = await provider.get_playlist_metadata(playlist)

        assert result is not None
        assert result.images is not None
        assert len(result.images) == 2  # THUMB and FANART

        # Check genres are included
        assert result.genres is not None
        assert result.genres == {"Rock", "Pop"}


@pytest.mark.asyncio
async def test_get_playlist_metadata_excludes_genres_when_disabled(
    tmp_path: Any,
) -> None:
    """Provider should not include genres when genre detection is disabled (default)."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()

    # Default config has genre detection disabled
    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key: {
            CONF_LOG_LEVEL: "GLOBAL",
            "template": "album_grid",
            "skip_provider_playlists": False,
            "enable_genre_detection": False,  # Disabled
            "genre_min_threshold": 10,
            "genre_max_count": 3,
        }.get(key)
    )

    playlist = _make_playlist()

    tracks = [
        _make_track_with_image(f"track{i}", f"http://example.com/img{i}.jpg") for i in range(10)
    ]

    # Add genres to tracks (should be ignored)
    for track in tracks:
        track.metadata.genres = {"Rock", "Pop"}

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
        mock_render.return_value = b"fake_image_data"

        result = await provider.get_playlist_metadata(playlist)

        assert result is not None
        assert result.images is not None
        # Genres should not be included
        assert result.genres is None
