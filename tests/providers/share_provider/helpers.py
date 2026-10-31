"""Shared builders for share provider tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.media_items import (
    Artist,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.helpers.compare import (
    TrackMatchConfidence,
)
from music_assistant.providers.share_provider import (
    ShareProvider,
)

if TYPE_CHECKING:
    from music_assistant_models.enums import ExternalID

ISRC = "USRC17607839"
BARCODE = "00724354283857"


def _provider() -> ShareProvider:
    """Build a share provider with mocked server services."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.domain = "share_provider"
    config = MagicMock()
    config.instance_id = "share_provider--test"
    config.name = "Share Links"
    config.get_value.return_value = "GLOBAL"
    provider = ShareProvider(mass, manifest, config)
    mass.config.get_provider_configs = AsyncMock(return_value=[])
    mass.music.tracks.enrich_provider_mappings = AsyncMock(return_value=SimpleNamespace(matches=()))
    return provider


def _mass(provider: ShareProvider) -> MagicMock:
    return cast("MagicMock", provider.mass)


def _core_match(track: Track, confidence: TrackMatchConfidence) -> SimpleNamespace:
    """Build a minimal core enrichment match for a provider track."""
    mapping = ProviderMapping(
        item_id=track.item_id,
        provider_domain=track.provider.split("--", maxsplit=1)[0],
        provider_instance=track.provider,
    )
    return SimpleNamespace(track=track, confidence=confidence, mapping=mapping)


def _track(
    item_id: str = "track",
    name: str = "Track",
    artist: str = "Artist",
    version: str = "",
    duration: int = 218,
    external_ids: set[tuple[ExternalID, str]] | None = None,
    provider_mappings: set[ProviderMapping] | None = None,
) -> Track:
    """Build a full-enough Track for exercising core comparison behavior."""
    return Track(
        item_id=item_id,
        provider="catalog--1",
        name=name,
        version=version,
        duration=duration,
        provider_mappings=provider_mappings
        if provider_mappings is not None
        else {
            ProviderMapping(
                item_id=item_id,
                provider_domain="catalog",
                provider_instance="catalog--1",
                available=True,
            )
        },
        external_ids=external_ids or set(),
        artists=UniqueList(
            [
                Artist(
                    item_id="artist",
                    provider="catalog--1",
                    name=artist,
                    provider_mappings=set(),
                )
            ]
            if artist
            else []
        ),
    )
