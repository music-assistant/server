"""Tests for the Cover Art Archive metadata provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import ResourceTemporarilyUnavailable

from music_assistant.providers.coverartarchive import (
    SUPPORTED_FEATURES,
    CoverArtArchiveMetadataProvider,
)
from tests.common import use_real_create_task

COVER_URL = "https://coverartarchive.org/release-group/mbid/front-1200"
IMAGE_URL = "https://archive.org/download/mbid-release/mbid-release-1_thumb1200.jpg"


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
    return CoverArtArchiveMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


def _response_cm(response: MagicMock) -> MagicMock:
    """Build a fake async context manager mimicking aiohttp's session.head()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _answer(
    provider: CoverArtArchiveMetadataProvider, status: int, location: str | None = None
) -> MagicMock:
    """Have every HEAD request to the archive answered with the status, return the HEAD mock."""
    response = MagicMock()
    response.status = status
    response.url = COVER_URL
    response.headers = {"Location": location} if location else {}
    head = MagicMock(return_value=_response_cm(response))
    provider.mass.http_session.head = head  # type: ignore[method-assign]
    return head


async def test_get_cover_art_url_is_the_redirect_location(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A redirect means the cover exists: its location is returned without following it."""
    head = _answer(provider, 307, IMAGE_URL)

    assert await provider._get_cover_art_url("mbid") == IMAGE_URL
    head.assert_called_once_with(COVER_URL, allow_redirects=False)


async def test_get_cover_art_url_returns_url_on_success(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A 200 response returns the resolved cover art URL."""
    _answer(provider, 200)

    assert await provider._get_cover_art_url("mbid") == COVER_URL


async def test_get_cover_art_url_without_a_cover_is_one_request(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A release group without a cover costs a single archive request, not one per size."""
    head = _answer(provider, 404)

    assert await provider._get_cover_art_url("mbid") is None
    head.assert_called_once_with(COVER_URL, allow_redirects=False)


async def test_get_cover_art_url_propagates_transient_error(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A 5xx failure surfaces as ResourceTemporarilyUnavailable, not cached as 'no cover art'."""
    _answer(provider, 503)

    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider._get_cover_art_url("mbid")


async def test_get_cover_art_url_propagates_a_timeout(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A request that times out surfaces as ResourceTemporarilyUnavailable."""
    provider.mass.http_session.head = MagicMock(  # type: ignore[method-assign]
        side_effect=TimeoutError
    )

    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider._get_cover_art_url("mbid")


async def test_get_cover_art_url_treats_a_redirect_without_location_as_transient(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A redirect that names no location says nothing about the cover, so it is transient."""
    _answer(provider, 307)

    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider._get_cover_art_url("mbid")
