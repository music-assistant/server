"""Tests for the Cover Art Archive metadata provider."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import RetriesExhausted

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.coverartarchive import (
    SUPPORTED_FEATURES,
    CoverArtArchiveMetadataProvider,
)
from tests.common import use_real_create_task

COVER_URL = "https://coverartarchive.org/release-group/mbid/front-1200"


@pytest.fixture
def provider() -> CoverArtArchiveMetadataProvider:
    """Return a provider with mocked dependencies and a cold cache."""
    mass = AsyncMock()
    mass.http_session = MagicMock()
    # force a cache miss so the wrapped fetch always runs
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    use_real_create_task(mass)
    manifest = MagicMock()
    manifest.domain = "coverartarchive"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    provider = CoverArtArchiveMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # the shared throttler paces lookups a second apart and backs off for minutes on a
    # failure; a test gets one of its own that does neither
    provider.throttler = ThrottlerManager(rate_limit=100, period=1, initial_backoff=0)
    return provider


def _response_cm(response: MagicMock) -> MagicMock:
    """Build a fake async context manager mimicking aiohttp's session.head()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _answer(provider: CoverArtArchiveMetadataProvider, status: int) -> MagicMock:
    """Have every HEAD request to the archive answered with the status, return the HEAD mock."""
    response = MagicMock()
    response.status = status
    response.url = COVER_URL
    if status >= 400:
        response.raise_for_status = MagicMock(side_effect=ClientError(f"status {status}"))
    head = MagicMock(return_value=_response_cm(response))
    provider.mass.http_session.head = head  # type: ignore[method-assign]
    return head


async def test_release_group_cover_url_is_the_resolved_url_on_success(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A 200 response returns the resolved cover art URL."""
    _answer(provider, 200)

    assert await provider.get_release_group_cover_url("mbid") == COVER_URL


async def test_release_group_cover_url_is_none_when_missing(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A 404 for every size means there is genuinely no cover art, so None is returned."""
    _answer(provider, 404)

    assert await provider.get_release_group_cover_url("mbid") is None


async def test_release_group_cover_url_retries_a_transient_error(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A 5xx failure is retried and then surfaces, never cached as 'no cover art'."""
    head = _answer(provider, 503)

    with pytest.raises(RetriesExhausted):
        await provider.get_release_group_cover_url("mbid")

    assert head.call_count == provider.throttler.retry_attempts
    cast("AsyncMock", provider.mass.cache.set).assert_not_awaited()


async def test_release_group_cover_url_takes_a_slot_of_the_shared_throttler(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """Every lookup passes the throttler holding the archive's one request per second."""
    assert CoverArtArchiveMetadataProvider.throttler.throttler.rate_limit == 1
    assert CoverArtArchiveMetadataProvider.throttler.throttler.period == 1
    _answer(provider, 404)

    with patch.object(provider.throttler, "acquire", wraps=provider.throttler.acquire) as acquire:
        await provider.get_release_group_cover_url("mbid")

    acquire.assert_called_once()


async def test_resolve_image_is_the_release_groups_cover_url(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """The image of a release group, given by its id, resolves to the archive's cover URL."""
    head = _answer(provider, 200)

    assert await provider.resolve_image("mbid") == COVER_URL
    head.assert_called_once_with(COVER_URL, allow_redirects=True)


async def test_resolve_image_is_none_without_a_cover(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A release group the archive has no cover for resolves to no image."""
    _answer(provider, 404)

    assert await provider.resolve_image("mbid") is None
