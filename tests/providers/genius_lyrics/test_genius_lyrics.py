"""Tests for the Genius lyrics metadata provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout

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
    """A transient Genius failure surfaces as ResourceTemporarilyUnavailable and is not cached."""
    provider._genius.search_song.side_effect = error
    cache_set = AsyncMock()
    provider.mass.cache.set = cache_set  # type: ignore[method-assign]

    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider.fetch_lyrics("Artist", "Song")
    cache_set.assert_not_called()


async def test_fetch_lyrics_returns_none_when_not_found(provider: GeniusProvider) -> None:
    """No search result means there are no lyrics, so None is returned."""
    provider._genius.search_song.return_value = None

    assert await provider.fetch_lyrics("Artist", "Song") is None
