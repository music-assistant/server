"""Tests for the Cover Art Archive metadata provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import ResourceTemporarilyUnavailable

from music_assistant.providers.coverartarchive import (
    SUPPORTED_FEATURES,
    CoverArtArchiveMetadataProvider,
)
from tests.common import use_real_create_task


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


async def test_get_cover_art_url_returns_url_on_success(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A 200 response returns the resolved cover art URL."""
    response = MagicMock()
    response.status = 200
    response.url = "https://coverartarchive.org/release-group/mbid/front-1200"
    provider.mass.http_session.head = MagicMock(  # type: ignore[method-assign]
        return_value=_response_cm(response)
    )

    result = await provider._get_cover_art_url("mbid")
    assert result == "https://coverartarchive.org/release-group/mbid/front-1200"


async def test_get_cover_art_url_returns_none_when_missing(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A 404 for every size means there is genuinely no cover art, so None is returned."""
    response = MagicMock()
    response.status = 404
    provider.mass.http_session.head = MagicMock(  # type: ignore[method-assign]
        return_value=_response_cm(response)
    )

    assert await provider._get_cover_art_url("mbid") is None


async def test_get_cover_art_url_propagates_transient_error(
    provider: CoverArtArchiveMetadataProvider,
) -> None:
    """A 5xx failure surfaces as ResourceTemporarilyUnavailable, not cached as 'no cover art'."""
    response = MagicMock()
    response.status = 503
    response.raise_for_status = MagicMock(side_effect=ClientError("service unavailable"))
    provider.mass.http_session.head = MagicMock(  # type: ignore[method-assign]
        return_value=_response_cm(response)
    )

    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider._get_cover_art_url("mbid")
