"""E2E tests for library synchronisation between providers and the MA database."""

from __future__ import annotations

import pytest

from tests.support.fixture_factory import make_track
from tests.support.harness import MusicAssistantHarness
from tests.support.mock_music_provider import MOCK_PROVIDER_DOMAIN, MockMusicProvider


@pytest.mark.asyncio
async def test_sync_adds_tracks_to_library(harness: MusicAssistantHarness) -> None:
    """Given a provider with three tracks, when synced, all tracks appear in the database."""
    # Given a mock provider containing three tracks
    tracks = [
        make_track(item_id=f"track-{i}", name=f"Track {i}", provider_domain=MOCK_PROVIDER_DOMAIN)
        for i in range(3)
    ]
    provider = MockMusicProvider(harness.mass, instance_id="sync_test_provider", tracks=tracks)

    # When the provider is registered and the library is synced
    await harness.add_provider(provider)
    await harness.sync_library(provider.instance_id)

    # Then all three tracks appear in the internal library
    library_tracks = await harness.mass.music.tracks.library_items()
    assert len(library_tracks) >= 3
    library_names = {t.name for t in library_tracks}
    assert "Track 0" in library_names
    assert "Track 1" in library_names
    assert "Track 2" in library_names


@pytest.mark.asyncio
async def test_sync_empty_provider_leaves_library_empty(harness: MusicAssistantHarness) -> None:
    """Given a provider with no tracks, when the library is synced, no tracks are added."""
    # Given a mock provider with no tracks
    provider = MockMusicProvider(harness.mass, instance_id="empty_provider", tracks=[])

    # When the provider is registered and synced
    await harness.add_provider(provider)
    await harness.sync_library(provider.instance_id)

    # Then the library remains empty
    library_tracks = await harness.mass.music.tracks.library_items()
    assert len(library_tracks) == 0


@pytest.mark.asyncio
async def test_sync_multiple_providers_aggregates_tracks(harness: MusicAssistantHarness) -> None:
    """Given two providers with distinct tracks, when synced, all tracks appear in library."""
    # Given two providers with two tracks each (distinct item IDs and names)
    tracks_a = [
        make_track(
            item_id=f"a-track-{i}",
            name=f"Provider A Track {i}",
            provider_domain=MOCK_PROVIDER_DOMAIN,
        )
        for i in range(2)
    ]
    tracks_b = [
        make_track(
            item_id=f"b-track-{i}",
            name=f"Provider B Track {i}",
            provider_domain=MOCK_PROVIDER_DOMAIN,
        )
        for i in range(2)
    ]
    provider_a = MockMusicProvider(harness.mass, instance_id="multi_provider_a", tracks=tracks_a)
    provider_b = MockMusicProvider(harness.mass, instance_id="multi_provider_b", tracks=tracks_b)

    # When both providers are registered and synced
    await harness.add_provider(provider_a)
    await harness.add_provider(provider_b)
    await harness.sync_library(provider_a.instance_id)
    await harness.sync_library(provider_b.instance_id)

    # Then tracks from both providers appear in the library
    library_tracks = await harness.mass.music.tracks.library_items()
    library_names = {t.name for t in library_tracks}
    assert "Provider A Track 0" in library_names
    assert "Provider A Track 1" in library_names
    assert "Provider B Track 0" in library_names
    assert "Provider B Track 1" in library_names
