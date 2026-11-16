"""Tests for the Genius lyrics metadata provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import RetriesExhausted
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout

from music_assistant.helpers.throttle_retry import ThrottlerManager
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
    # the shared throttler paces lookups a second apart and backs off on a failure;
    # a test gets one of its own that does neither
    prov.throttler = ThrottlerManager(rate_limit=100, period=1, initial_backoff=0)
    return prov


@pytest.mark.parametrize(
    "error",
    [
        Timeout("timed out"),
        RequestsConnectionError("network down"),
        AssertionError("Unexpected response status code: 503."),
    ],
)
async def test_fetch_lyrics_raises_temporary_error(
    provider: GeniusProvider, error: Exception
) -> None:
    """A transient Genius failure is retried, raised and not cached, without a rate limit."""
    provider._genius.search_song.side_effect = error
    cache_set = AsyncMock()
    provider.mass.cache.set = cache_set  # type: ignore[method-assign]

    with pytest.raises(RetriesExhausted):
        await provider.fetch_lyrics("Artist", "Song")
    cache_set.assert_not_called()
    assert not provider.rate_limited


async def test_fetch_lyrics_takes_a_slot_of_the_shared_throttler(provider: GeniusProvider) -> None:
    """Every lookup passes the throttler holding Genius to one request per second."""
    assert GeniusProvider.throttler.throttler.rate_limit == 1
    assert GeniusProvider.throttler.throttler.period == 1
    provider._genius.search_song.return_value = None

    with patch.object(provider.throttler, "acquire", wraps=provider.throttler.acquire) as acquire:
        await provider.fetch_lyrics("Artist", "Song")

    acquire.assert_called_once()


async def test_fetch_lyrics_rate_limit_arms_the_cooldown(provider: GeniusProvider) -> None:
    """A 429 from Genius holds back every further lookup and reports the provider rate limited."""
    provider.throttler = ThrottlerManager(
        rate_limit=100, period=1, retry_attempts=1, initial_backoff=60
    )
    provider._genius.search_song.side_effect = AssertionError(
        "Unexpected response status code: 429. Expected 200 or 204."
    )
    cache_set = AsyncMock()
    provider.mass.cache.set = cache_set  # type: ignore[method-assign]

    with pytest.raises(RetriesExhausted):
        await provider.fetch_lyrics("Artist", "Song")

    cache_set.assert_not_called()
    assert provider.throttler.cooldown_remaining > 0
    assert provider.rate_limited


async def test_rate_limited_follows_the_throttler_cooldown(provider: GeniusProvider) -> None:
    """The provider reports a rate limit exactly while its throttler holds requests back."""
    assert not provider.rate_limited

    provider.throttler.set_cooldown(60)

    assert provider.rate_limited


async def test_fetch_lyrics_keeps_other_assertion_errors(provider: GeniusProvider) -> None:
    """An AssertionError unrelated to the API response status is not treated as temporary."""
    provider._genius.search_song.side_effect = AssertionError("parser invariant")

    with pytest.raises(AssertionError):
        await provider.fetch_lyrics("Artist", "Song")


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
