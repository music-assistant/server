"""Tests for the LRCLIB metadata provider."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import MusicAssistantError

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.lrclib import SUPPORTED_FEATURES, LrclibProvider


@pytest.fixture
def provider() -> LrclibProvider:
    """Return an LrclibProvider with mocked dependencies and a cold cache."""
    mass = AsyncMock()
    mass.http_session = MagicMock()
    # force a cache miss so the wrapped fetch always runs, and swallow the store task
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.create_task = MagicMock(side_effect=lambda coro, **_: coro.close())
    manifest = MagicMock()
    manifest.domain = "lrclib"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    prov = LrclibProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov.api_url = "https://lrclib.net/api"
    # retry_attempts=1 keeps the transient-error test fast (no backoff sleeps)
    prov.throttler = ThrottlerManager(rate_limit=1, period=1, retry_attempts=1)
    return prov


def _response_cm(*, response: MagicMock | None = None, exc: Exception | None = None) -> MagicMock:
    """Build a fake async context manager mimicking aiohttp's session.get()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response, side_effect=exc)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_get_data_returns_none_when_not_found(provider: LrclibProvider) -> None:
    """A 404 means there are genuinely no lyrics, so None is returned (and cached)."""
    response = MagicMock()
    response.status = 404
    response.raise_for_status = MagicMock()
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_response_cm(response=response)
    )

    assert await provider._get_data(track_name="Song", artist_name="Artist") is None


async def test_get_data_propagates_transient_error(provider: LrclibProvider) -> None:
    """A network failure surfaces as a MusicAssistantError instead of being cached as 'no lyrics'."""
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_response_cm(exc=ClientError("network down"))
    )

    with pytest.raises(MusicAssistantError):
        await provider._get_data(track_name="Song", artist_name="Artist")
