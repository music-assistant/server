"""Tests for playlist artwork integration in metadata controller."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ImageType, ProviderFeature
from music_assistant_models.errors import ProviderUnavailableError
from music_assistant_models.media_items import MediaItemImage, Playlist, ProviderMapping, Track
from music_assistant_models.media_items.metadata import MediaItemMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.metadata.enrichment import MetadataEnrichmentMixin


async def _empty_tracks_iter(
    _item_id: str, _provider: str, *_args: Any, **_kwargs: Any
) -> AsyncGenerator[Track]:
    """Empty async generator for mocking playlist tracks."""
    if False:  # pragma: no cover
        yield  # type: ignore[unreachable]


def _make_playlist() -> Playlist:
    """Create a test playlist."""
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


@pytest.mark.asyncio
async def test_update_playlist_metadata_calls_provider(tmp_path: Any) -> None:
    """_update_playlist_metadata should call get_playlist_metadata on providers."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment._collage_images_dir = str(tmp_path / "collage")
    enrichment.create_collage_image = AsyncMock(return_value=None)  # type: ignore[method-assign]

    # Mock metadata provider
    provider = MagicMock()
    provider.name = "playlist_metadata"
    provider.supported_features = {ProviderFeature.PLAYLIST_METADATA}
    provider.get_playlist_metadata = AsyncMock(
        return_value=MediaItemMetadata(
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path="/fake/thumb.jpg",
                        provider="playlist_metadata",
                        remotely_accessible=False,
                    ),
                    MediaItemImage(
                        type=ImageType.FANART,
                        path="/fake/fanart.jpg",
                        provider="playlist_metadata",
                        remotely_accessible=False,
                    ),
                ]
            )
        )
    )
    enrichment.providers = [provider]  # type: ignore[misc]

    playlist = _make_playlist()
    enrichment.mass.music.playlists.tracks = _empty_tracks_iter
    enrichment.mass.music.playlists.update_item_in_library = AsyncMock()

    await enrichment._update_playlist_metadata(playlist, force_refresh=False)

    provider.get_playlist_metadata.assert_called_once_with(playlist)
    assert any(img.provider == "playlist_metadata" for img in (playlist.metadata.images or []))


@pytest.mark.asyncio
async def test_update_playlist_metadata_handles_provider_exception(tmp_path: Any) -> None:
    """_update_playlist_metadata should handle MusicAssistantError exceptions from providers gracefully."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment._collage_images_dir = str(tmp_path / "collage")
    enrichment.create_collage_image = AsyncMock(return_value=None)  # type: ignore[method-assign]

    # Mock metadata provider that raises MusicAssistantError
    provider = MagicMock()
    provider.name = "playlist_metadata"
    provider.supported_features = {ProviderFeature.PLAYLIST_METADATA}
    provider.get_playlist_metadata = AsyncMock(
        side_effect=ProviderUnavailableError("Test provider unavailable")
    )
    enrichment.providers = [provider]  # type: ignore[misc]

    playlist = _make_playlist()
    enrichment.mass.music.playlists.tracks = _empty_tracks_iter
    enrichment.mass.music.playlists.update_item_in_library = AsyncMock()

    # Should not raise, just log warning
    await enrichment._update_playlist_metadata(playlist, force_refresh=False)

    enrichment.logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_update_playlist_metadata_preserves_existing_thumb(tmp_path: Any) -> None:
    """_update_playlist_metadata should preserve existing non-collage thumb when providers return None."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment._collage_images_dir = str(tmp_path / "collage")
    enrichment.create_collage_image = AsyncMock(return_value=None)  # type: ignore[method-assign]

    # Mock metadata provider that returns None
    provider = MagicMock()
    provider.name = "playlist_metadata"
    provider.supported_features = {ProviderFeature.PLAYLIST_METADATA}
    provider.get_playlist_metadata = AsyncMock(return_value=None)
    enrichment.providers = [provider]  # type: ignore[misc]

    playlist = _make_playlist()
    existing_thumb = MediaItemImage(
        type=ImageType.THUMB,
        path="/existing/thumb.jpg",
        provider="some_provider",
        remotely_accessible=False,
    )
    playlist.metadata.images = UniqueList([existing_thumb])

    enrichment.mass.music.playlists.tracks = _empty_tracks_iter
    enrichment.mass.music.playlists.update_item_in_library = AsyncMock()

    await enrichment._update_playlist_metadata(playlist, force_refresh=False)

    # Existing thumb should be preserved
    assert any(
        img.type == ImageType.THUMB and img.path == "/existing/thumb.jpg"
        for img in (playlist.metadata.images or [])
    )


@pytest.mark.asyncio
async def test_update_playlist_metadata_preserves_collage_thumb_when_no_new_generated(
    tmp_path: Any,
) -> None:
    """_update_playlist_metadata should preserve existing collage thumb when no new thumb is generated."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment._collage_images_dir = str(tmp_path / "collage")
    os.makedirs(enrichment._collage_images_dir, exist_ok=True)
    enrichment.create_collage_image = AsyncMock()  # type: ignore[method-assign]

    # Mock metadata provider that returns None
    provider = MagicMock()
    provider.name = "playlist_metadata"
    provider.supported_features = {ProviderFeature.PLAYLIST_METADATA}
    provider.get_playlist_metadata = AsyncMock(return_value=None)
    enrichment.providers = [provider]  # type: ignore[misc]

    playlist = _make_playlist()
    # Existing thumb is a collage
    old_collage_thumb = MediaItemImage(
        type=ImageType.THUMB,
        path=f"{enrichment._collage_images_dir}/old_thumb.jpg",
        provider="builtin",
        remotely_accessible=False,
    )
    playlist.metadata.images = UniqueList([old_collage_thumb])

    enrichment.mass.music.playlists.tracks = _empty_tracks_iter
    enrichment.mass.music.playlists.update_item_in_library = AsyncMock()

    await enrichment._update_playlist_metadata(playlist, force_refresh=False)

    # Should preserve old collage, not generate new thumb collage
    assert any(
        img.type == ImageType.THUMB and img.path == old_collage_thumb.path
        for img in (playlist.metadata.images or [])
    )
    # create_collage_image may be called for fanart, but not for thumb
    if enrichment.create_collage_image.called:
        # All calls should be for fanart=True, never for thumb (fanart=False)
        for call in enrichment.create_collage_image.call_args_list:
            assert call.kwargs.get("fanart") is True


@pytest.mark.asyncio
async def test_update_playlist_metadata_skips_providers_without_feature(tmp_path: Any) -> None:
    """_update_playlist_metadata should skip providers without PLAYLIST_METADATA feature."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment._collage_images_dir = str(tmp_path / "collage")
    enrichment.create_collage_image = AsyncMock(return_value=None)  # type: ignore[method-assign]

    # Provider without PLAYLIST_METADATA feature
    provider_without_feature = MagicMock()
    provider_without_feature.name = "other_provider"
    provider_without_feature.supported_features = set()

    enrichment.providers = [provider_without_feature]  # type: ignore[misc]

    playlist = _make_playlist()
    enrichment.mass.music.playlists.tracks = _empty_tracks_iter
    enrichment.mass.music.playlists.update_item_in_library = AsyncMock()

    # Should not raise, just skip the provider
    await enrichment._update_playlist_metadata(playlist, force_refresh=False)

    # Should have called update_item_in_library
    enrichment.mass.music.playlists.update_item_in_library.assert_called_once()


@pytest.mark.asyncio
async def test_update_playlist_metadata_calls_providers_for_dynamic_playlists(
    tmp_path: Any,
) -> None:
    """
    _update_playlist_metadata should call providers even for dynamic playlists.

    Providers decide themselves whether to return metadata (e.g., playlist_metadata
    provider skips provider playlists via CONF_SKIP_PROVIDER_PLAYLISTS).
    """
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment._collage_images_dir = str(tmp_path / "collage")

    # Mock metadata provider that returns None for dynamic playlists
    provider = MagicMock()
    provider.name = "playlist_metadata"
    provider.supported_features = {ProviderFeature.PLAYLIST_METADATA}
    provider.get_playlist_metadata = AsyncMock(return_value=None)
    enrichment.providers = [provider]  # type: ignore[misc]

    # Mock playlist tracks iterator (returns no tracks)
    async def mock_tracks(
        item_id: str,  # noqa: ARG001
        provider: str,  # noqa: ARG001
    ) -> AsyncIterator[Track]:
        """Empty async generator."""
        if False:
            yield  # type: ignore[unreachable]  # pragma: no cover

    enrichment.mass.music.playlists.tracks = mock_tracks

    # Mock update_item_in_library
    enrichment.mass.music.playlists.update_item_in_library = AsyncMock()

    # Create dynamic playlist (e.g., Pandora station, Apple Music station)
    playlist = Playlist(
        item_id="dynamic_station_1",
        provider="pandora",
        name="Dynamic Station",
        is_dynamic=True,
        provider_mappings={
            ProviderMapping(
                item_id="dynamic_station_1",
                provider_domain="pandora",
                provider_instance="pandora_instance",
            )
        },
        metadata=MediaItemMetadata(),
    )

    await enrichment._update_playlist_metadata(playlist, force_refresh=True)

    # Provider should be called (provider decides whether to return metadata)
    provider.get_playlist_metadata.assert_called_once_with(playlist)


@pytest.mark.asyncio
async def test_update_playlist_metadata_allows_smart_playlists(tmp_path: Any) -> None:
    """_update_playlist_metadata should allow smart playlists despite is_dynamic=True."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment._collage_images_dir = str(tmp_path / "collage")
    enrichment.create_collage_image = AsyncMock(return_value=None)  # type: ignore[method-assign]

    # Mock metadata provider
    provider = MagicMock()
    provider.name = "playlist_metadata"
    provider.supported_features = {ProviderFeature.PLAYLIST_METADATA}
    provider.get_playlist_metadata = AsyncMock(
        return_value=MediaItemMetadata(
            images=UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path="/fake/thumb.jpg",
                        provider="playlist_metadata",
                        remotely_accessible=False,
                    )
                ]
            )
        )
    )
    enrichment.providers = [provider]  # type: ignore[misc]

    # Create smart playlist with is_dynamic=True
    playlist = Playlist(
        item_id="smart_playlist_1",
        provider="library",
        name="Smart Playlist",
        is_dynamic=True,  # This test covers the dynamic smart-playlist exception path
        provider_mappings={
            ProviderMapping(
                item_id="smart_playlist_1",
                provider_domain="smart_playlist",
                provider_instance="smart_playlist",
            )
        },
        metadata=MediaItemMetadata(),
    )

    enrichment.mass.music.playlists.tracks = _empty_tracks_iter
    enrichment.mass.music.playlists.update_item_in_library = AsyncMock()

    await enrichment._update_playlist_metadata(playlist, force_refresh=True)

    # Should have called provider.get_playlist_metadata despite is_dynamic=True
    provider.get_playlist_metadata.assert_called_once_with(playlist)
    assert any(img.provider == "playlist_metadata" for img in (playlist.metadata.images or []))
