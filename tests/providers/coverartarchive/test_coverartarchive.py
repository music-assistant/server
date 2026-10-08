"""Tests for the Cover Art Archive metadata provider."""

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.errors import RetriesExhausted

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.coverartarchive import (
    SUPPORTED_FEATURES,
    CoverArtArchiveMetadataProvider,
)
from tests.common import use_real_create_task

COVER_URL = "https://coverartarchive.org/release-group/mbid/front-1200"
IMAGE_URL = "https://archive.org/download/mbid-release/mbid-release-1_thumb1200.jpg"
SMALL_IMAGE_URL = "https://archive.org/download/mbid-release/mbid-release-1_thumb500.jpg"


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


def _response(status: int, location: str | None = None) -> MagicMock:
    """Build a fake archive response with the status and, optionally, a Location header."""
    response = MagicMock()
    response.status = status
    response.url = COVER_URL
    response.headers = {"Location": location} if location else {}
    return response


def _answer(
    provider: CoverArtArchiveMetadataProvider, status: int, location: str | None = None
) -> MagicMock:
    """Have every HEAD request to the archive answered with the status, return the HEAD mock."""
    head = MagicMock(return_value=_response_cm(_response(status, location)))
    provider.mass.http_session.head = head  # type: ignore[method-assign]
    return head


async def test_release_group_cover_url_is_the_redirect_location(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A redirect means the cover exists: its location is returned without following it."""
    head = _answer(provider, 307, IMAGE_URL)

    assert await provider.get_release_group_cover_url("mbid") == IMAGE_URL
    head.assert_called_once_with(COVER_URL, allow_redirects=False)


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


async def test_release_group_cover_url_retries_a_timeout(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A request that times out is retried and then surfaces, never cached as 'no cover art'."""
    head = MagicMock(side_effect=TimeoutError)
    provider.mass.http_session.head = head  # type: ignore[method-assign]

    with pytest.raises(RetriesExhausted):
        await provider.get_release_group_cover_url("mbid")

    assert head.call_count == provider.throttler.retry_attempts
    cast("AsyncMock", provider.mass.cache.set).assert_not_awaited()


async def test_release_group_cover_url_retries_a_redirect_without_location(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A redirect that names no location says nothing about the cover, so it is transient."""
    _answer(provider, 307)

    with pytest.raises(RetriesExhausted):
        await provider.get_release_group_cover_url("mbid")

    cast("AsyncMock", provider.mass.cache.set).assert_not_awaited()


async def test_release_group_cover_url_falls_back_to_the_small_cover(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """Without a large cover, the small cover's redirect location is returned."""
    provider.mass.http_session.head = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda url, **_kwargs: _response_cm(
            _response(404) if url.endswith("front-1200") else _response(307, SMALL_IMAGE_URL)
        )
    )

    assert await provider.get_release_group_cover_url("mbid") == SMALL_IMAGE_URL


async def test_release_group_cover_url_takes_a_slot_of_the_shared_throttler(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """Every request passes the throttler holding the archive's one request per second."""
    assert CoverArtArchiveMetadataProvider.throttler.throttler.rate_limit == 1
    assert CoverArtArchiveMetadataProvider.throttler.throttler.period == 1
    _answer(provider, 200)

    with patch.object(provider.throttler, "acquire", wraps=provider.throttler.acquire) as acquire:
        await provider.get_release_group_cover_url("mbid")

    acquire.assert_called_once()


async def test_release_group_cover_url_fallback_takes_a_slot_per_request(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """Falling back from the large to the small cover is two archive requests, two slots."""
    provider.mass.http_session.head = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda url, **_kwargs: _response_cm(
            _response(404) if url.endswith("front-1200") else _response(200)
        )
    )

    with patch.object(provider.throttler, "acquire", wraps=provider.throttler.acquire) as acquire:
        assert await provider.get_release_group_cover_url("mbid") == COVER_URL

    assert acquire.call_count == 2


async def test_resolve_image_is_the_release_groups_cover_url(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """The image of a release group, given by its id, resolves to the archive's cover URL."""
    head = _answer(provider, 307, IMAGE_URL)

    assert await provider.resolve_image("mbid") == IMAGE_URL
    head.assert_called_once_with(COVER_URL, allow_redirects=False)


async def test_resolve_image_is_none_without_a_cover(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A release group the archive has no cover for resolves to no image."""
    _answer(provider, 404)

    assert await provider.resolve_image("mbid") is None
