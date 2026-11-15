"""Tests for the iTunes Artwork metadata provider."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from music_assistant_models.errors import RetriesExhausted

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.itunes_artwork import (
    SUPPORTED_FEATURES,
    ITunesArtworkMetadataProvider,
)
from tests.common import use_real_create_task

BARCODE = "0602537351169"
ARTWORK_URL = "https://is1-ssl.mzstatic.com/image/thumb/Music/source/100x100bb.jpg"


@pytest.fixture
def provider() -> ITunesArtworkMetadataProvider:
    """Return a provider with mocked dependencies and a cold cache."""
    mass = AsyncMock()
    mass.http_session = MagicMock()
    # force a cache miss so the wrapped lookup always runs
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    use_real_create_task(mass)
    manifest = MagicMock()
    manifest.domain = "itunes_artwork"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    provider = ITunesArtworkMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # the shared throttler paces lookups three seconds apart and backs off on a failure;
    # a test gets one of its own that does neither
    provider.throttler = ThrottlerManager(rate_limit=100, period=1, initial_backoff=0)
    return provider


def _answer(
    provider: ITunesArtworkMetadataProvider,
    status: int,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Have every lookup answered with the status and JSON body, return the GET mock."""
    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    response.json = AsyncMock(return_value=data or {})
    if status >= 400:
        response.raise_for_status.side_effect = aiohttp.ClientResponseError(
            MagicMock(), (), status=status
        )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    get = MagicMock(return_value=cm)
    provider.mass.http_session.get = get  # type: ignore[method-assign]
    return get


async def test_artwork_url_is_the_high_resolution_artwork(
    provider: ITunesArtworkMetadataProvider,
) -> None:
    """A barcode found on iTunes resolves to the 1500px version of its artwork."""
    _answer(provider, 200, {"resultCount": 1, "results": [{"artworkUrl100": ARTWORK_URL}]})

    assert await provider._get_artwork_url(BARCODE) == ARTWORK_URL.replace(
        "100x100bb", "1500x1500bb"
    )


async def test_artwork_url_takes_a_slot_of_the_shared_throttler(
    provider: ITunesArtworkMetadataProvider,
) -> None:
    """Every lookup passes the throttler holding iTunes to 20 requests per minute."""
    assert ITunesArtworkMetadataProvider.throttler.throttler.rate_limit == 20
    assert ITunesArtworkMetadataProvider.throttler.throttler.period == 60
    _answer(provider, 200, {"resultCount": 0, "results": []})

    with patch.object(provider.throttler, "acquire", wraps=provider.throttler.acquire) as acquire:
        assert await provider._get_artwork_url(BARCODE) is None

    acquire.assert_called_once()


@pytest.mark.parametrize("status", [403, 429])
async def test_refusal_arms_the_cooldown_from_retry_after(
    provider: ITunesArtworkMetadataProvider, status: int
) -> None:
    """A refusal by iTunes holds back every lookup for as long as its Retry-After asks."""
    _answer(provider, status, headers={"Retry-After": "300"})

    with pytest.raises(RetriesExhausted):
        await provider._get_artwork_url(BARCODE)

    assert provider.throttler.cooldown_remaining > 290
    assert provider.rate_limited


@pytest.mark.parametrize("status", [403, 429])
async def test_refusal_without_retry_after_arms_the_cooldown(
    provider: ITunesArtworkMetadataProvider, status: int
) -> None:
    """A refusal by iTunes without a Retry-After still reports the provider rate limited."""
    provider.throttler = ThrottlerManager(
        rate_limit=100, period=1, retry_attempts=1, initial_backoff=60
    )
    _answer(provider, status)

    with pytest.raises(RetriesExhausted):
        await provider._get_artwork_url(BARCODE)

    assert provider.rate_limited


async def test_server_error_is_temporary_without_a_rate_limit(
    provider: ITunesArtworkMetadataProvider,
) -> None:
    """An ordinary iTunes failure is retried, raised and not cached, without a rate limit."""
    get = _answer(provider, 500)
    cache_set = AsyncMock()
    provider.mass.cache.set = cache_set  # type: ignore[method-assign]

    with pytest.raises(RetriesExhausted):
        await provider._get_artwork_url(BARCODE)

    assert get.call_count == provider.throttler.retry_attempts
    cache_set.assert_not_called()
    assert not provider.rate_limited


async def test_rate_limited_follows_the_throttler_cooldown(
    provider: ITunesArtworkMetadataProvider,
) -> None:
    """The provider reports a rate limit exactly while its throttler holds requests back."""
    assert not provider.rate_limited

    provider.throttler.set_cooldown(60)

    assert provider.rate_limited
