"""Tests for metadata enrichment resilience to provider failures."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import ExternalID, ProviderFeature
from music_assistant_models.media_items import Album, Artist, Track
from music_assistant_models.media_items.metadata import MediaItemMetadata

from music_assistant.controllers.metadata.constants import CONF_ENABLE_ONLINE_METADATA
from music_assistant.controllers.metadata.enrichment import MetadataEnrichmentMixin


def _online_metadata_only(key: str, *_args: Any, **_kwargs: Any) -> bool:
    """Config stub: only online metadata is enabled (prefer-local-genres off)."""
    return key == CONF_ENABLE_ONLINE_METADATA


def _metadata_provider(name: str, feature: ProviderFeature) -> MagicMock:
    """Return a mock metadata provider advertising a single metadata feature."""
    provider = MagicMock()
    provider.name = name
    provider.priority = 0
    provider.supported_features = {feature}
    return provider


@pytest.mark.asyncio
async def test_album_enrichment_survives_provider_error() -> None:
    """A raising album metadata provider is logged and skipped; later providers still run."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment.config = MagicMock()
    enrichment.config.get_value = _online_metadata_only
    enrichment.mass.music.albums.update_item_in_library = AsyncMock()

    boom = _metadata_provider("boom", ProviderFeature.ALBUM_METADATA)
    boom.get_album_metadata = AsyncMock(side_effect=aiohttp.ClientError("network down"))
    good = _metadata_provider("good", ProviderFeature.ALBUM_METADATA)
    good.get_album_metadata = AsyncMock(return_value=None)
    enrichment.providers = [boom, good]  # type: ignore[misc]

    album = Album(
        item_id="1",
        provider="library",
        name="Test Album",
        provider_mappings=set(),
        metadata=MediaItemMetadata(),
    )

    # a transient provider error must not abort enrichment
    await enrichment._update_album_metadata(album, force_refresh=True)

    boom.get_album_metadata.assert_awaited_once()
    good.get_album_metadata.assert_awaited_once()  # loop continued past the failing provider
    enrichment.logger.warning.assert_called_once()
    enrichment.mass.music.albums.update_item_in_library.assert_awaited_once()


@pytest.mark.asyncio
async def test_track_enrichment_survives_provider_error() -> None:
    """A raising track metadata provider is logged and skipped; later providers still run."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment.config = MagicMock()
    enrichment.config.get_value = _online_metadata_only
    enrichment.mass.music.tracks.update_item_in_library = AsyncMock()

    boom = _metadata_provider("boom", ProviderFeature.TRACK_METADATA)
    boom.get_track_metadata = AsyncMock(side_effect=aiohttp.ClientError("network down"))
    good = _metadata_provider("good", ProviderFeature.TRACK_METADATA)
    good.get_track_metadata = AsyncMock(return_value=None)
    enrichment.providers = [boom, good]  # type: ignore[misc]

    track = Track(
        item_id="1",
        provider="library",
        name="Test Track",
        provider_mappings=set(),
        metadata=MediaItemMetadata(),
    )

    await enrichment._update_track_metadata(track, force_refresh=True)

    boom.get_track_metadata.assert_awaited_once()
    good.get_track_metadata.assert_awaited_once()
    enrichment.logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_artist_enrichment_survives_provider_error() -> None:
    """A raising artist metadata provider is logged and skipped; later providers still run."""
    enrichment = MetadataEnrichmentMixin()
    enrichment.logger = MagicMock()
    enrichment.mass = MagicMock()
    enrichment.config = MagicMock()
    enrichment.config.get_value = _online_metadata_only
    enrichment.preferred_language = "en"  # type: ignore[misc]
    enrichment.mass.music.artists.update_item_in_library = AsyncMock()

    boom = _metadata_provider("boom", ProviderFeature.ARTIST_METADATA)
    boom.get_artist_metadata = AsyncMock(side_effect=aiohttp.ClientError("network down"))
    good = _metadata_provider("good", ProviderFeature.ARTIST_METADATA)
    good.get_artist_metadata = AsyncMock(return_value=None)
    enrichment.providers = [boom, good]  # type: ignore[misc]

    artist = Artist(
        item_id="1",
        provider="library",
        name="Test Artist",
        provider_mappings=set(),
        external_ids={(ExternalID.MB_ARTIST, "11111111-1111-1111-1111-111111111111")},
        metadata=MediaItemMetadata(),
    )

    await enrichment._update_artist_metadata(artist, force_refresh=True)

    boom.get_artist_metadata.assert_awaited_once()
    good.get_artist_metadata.assert_awaited_once()
    enrichment.logger.warning.assert_called_once()
