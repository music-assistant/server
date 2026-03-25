"""E2E tests for search across music providers."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType

from tests.support.fixture_factory import make_album, make_artist, make_track
from tests.support.harness import MusicAssistantHarness
from tests.support.mock_music_provider import MOCK_PROVIDER_DOMAIN, MockMusicProvider


@pytest.mark.asyncio
async def test_search_returns_matching_tracks(harness: MusicAssistantHarness) -> None:
    """Given a provider with named tracks, when searching by substring, the track is returned."""
    # Given a provider containing a distinctively-named track
    track = make_track(
        item_id="beatles-1", name="Abbey Road Song", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    provider = MockMusicProvider(harness.mass, instance_id="search_provider_tracks", tracks=[track])
    await harness.add_provider(provider)

    # When searching for a substring of the track name
    results = await harness.mass.music.search("Abbey Road", [MediaType.TRACK])

    # Then the matching track is present in the results
    assert any("Abbey Road" in t.name for t in results.tracks)


@pytest.mark.asyncio
async def test_search_returns_empty_for_unknown_query(harness: MusicAssistantHarness) -> None:
    """Given a provider with tracks, when searching for a non-matching term, results are empty."""
    # Given a provider with a track whose name does not contain the query
    track = make_track(
        item_id="known-1", name="Known Track Title", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    provider = MockMusicProvider(harness.mass, instance_id="search_provider_empty", tracks=[track])
    await harness.add_provider(provider)

    # When searching for a term that matches no track
    results = await harness.mass.music.search("zzz_no_match_xyz", [MediaType.TRACK])

    # Then no tracks are returned
    assert results.tracks == []


@pytest.mark.asyncio
async def test_search_across_multiple_providers(harness: MusicAssistantHarness) -> None:
    """Given two providers with distinct tracks, when searching, results from both are included."""
    # Given two providers with distinctively-named tracks
    track_a = make_track(
        item_id="mp-a-1", name="Jazz from Provider Alpha", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    track_b = make_track(
        item_id="mp-b-1", name="Jazz from Provider Beta", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    provider_a = MockMusicProvider(harness.mass, instance_id="search_multi_a", tracks=[track_a])
    provider_b = MockMusicProvider(harness.mass, instance_id="search_multi_b", tracks=[track_b])
    await harness.add_provider(provider_a)
    await harness.add_provider(provider_b)

    # When searching for a term present in both track names
    results = await harness.mass.music.search("Jazz", [MediaType.TRACK])

    # Then tracks from both providers are returned
    result_names = {t.name for t in results.tracks}
    assert "Jazz from Provider Alpha" in result_names
    assert "Jazz from Provider Beta" in result_names


@pytest.mark.asyncio
async def test_search_returns_albums_and_artists(harness: MusicAssistantHarness) -> None:
    """Given a provider with track/album/artist sharing a keyword, all three types are returned."""
    # Given a provider with a track, album, and artist each containing the keyword "Cosmic"
    track = make_track(
        item_id="cosmic-t", name="Cosmic Journey", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    album = make_album(
        item_id="cosmic-al", name="Cosmic Album", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    artist = make_artist(
        item_id="cosmic-ar", name="Cosmic Artist", provider_domain=MOCK_PROVIDER_DOMAIN
    )
    provider = MockMusicProvider(
        harness.mass,
        instance_id="search_media_types",
        tracks=[track],
        albums=[album],
        artists=[artist],
    )
    await harness.add_provider(provider)

    # When searching across all media types
    results = await harness.mass.music.search(
        "Cosmic", [MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST]
    )

    # Then track, album, and artist results each contain the keyword
    assert any("Cosmic" in t.name for t in results.tracks)
    assert any("Cosmic" in a.name for a in results.albums)
    assert any("Cosmic" in a.name for a in results.artists)
