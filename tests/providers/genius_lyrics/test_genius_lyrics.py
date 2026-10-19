"""Tests for the Genius lyrics metadata provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.genius_lyrics import SUPPORTED_FEATURES, GeniusProvider
from tests.common import use_real_create_task


@pytest.fixture
def provider() -> GeniusProvider:
    """Return a GeniusProvider with a mocked Genius client and a cold cache."""
    mass = AsyncMock()
    # force a cache miss so the wrapped fetch always runs
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    use_real_create_task(mass)
    manifest = MagicMock()
    manifest.domain = "genius_lyrics"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    prov = GeniusProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov._genius = MagicMock()
    return prov


async def test_fetch_lyrics_returns_none_when_not_found(provider: GeniusProvider) -> None:
    """No search result means there are no lyrics, so None is returned."""
    provider._genius.search_song.return_value = None

    assert await provider.fetch_lyrics("Artist", "Song") is None


@pytest.mark.parametrize(
    ("artist", "title", "hit_artist", "hit_title"),
    [
        ("Wes Montgomery", "No Blues", "Wes Montgomery", "Twisted Blues"),
        ("Wes Montgomery", "If You Could See Me Now", "SK8", "Cool Kids"),
        ("U2", "One", "U2", "Gone"),
        ("America", "A Horse with No Name", "American Authors", "A Horse with No Name"),
    ],
)
async def test_fetch_lyrics_rejects_mismatched_search_hit(
    provider: GeniusProvider, artist: str, title: str, hit_artist: str, hit_title: str
) -> None:
    """A search hit for a different song or artist is rejected instead of returned as lyrics."""
    provider._genius.search_song.return_value = MagicMock(
        title=hit_title, artist=hit_artist, lyrics="Wrong lyrics"
    )

    assert await provider.fetch_lyrics(artist, title) is None


@pytest.mark.parametrize(
    ("artist", "title", "hit_artist", "hit_title"),
    [
        ("Wes Montgomery", "No Blues (Live)", "Wes Montgomery", "No Blues"),
        ("Queen", "Bohemian Rhapsody - Remastered 2011", "Queen", "Bohemian Rhapsody"),
        ("JAY-Z", "Otis", "JAY-Z & Kanye West", "Otis"),
    ],
)
async def test_fetch_lyrics_accepts_matching_search_hit(
    provider: GeniusProvider, artist: str, title: str, hit_artist: str, hit_title: str
) -> None:
    """A search hit matching the requested title and artist returns its lyrics."""
    provider._genius.search_song.return_value = MagicMock(
        title=hit_title, artist=hit_artist, lyrics="Right lyrics", pyongs_count=0
    )

    assert await provider.fetch_lyrics(artist, title) == "Right lyrics"
