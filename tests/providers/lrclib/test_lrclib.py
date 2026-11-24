"""Tests for the LRCLIB metadata provider."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from music_assistant_models.errors import MusicAssistantError

from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers.lrclib import SUPPORTED_FEATURES, LrclibProvider
from tests.common import use_real_create_task
from tests.controllers.music.helpers import create_track


@pytest.fixture
def provider() -> LrclibProvider:
    """Return an LrclibProvider with mocked dependencies and a cold cache."""
    mass = AsyncMock()
    mass.http_session = MagicMock()
    # force a cache miss so the wrapped fetch always runs
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    use_real_create_task(mass)
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


async def test_get_track_metadata_looks_up_lrc_when_plain_lyrics_exist(
    provider: LrclibProvider,
) -> None:
    """Existing plain lyrics do not prevent looking up synchronized lyrics."""
    track = create_track("spotify", "track")
    track.metadata.lyrics = "plain lyrics"
    provider._get_data = AsyncMock(  # type: ignore[method-assign]
        return_value={"syncedLyrics": "[00:01.00]sync"}
    )

    metadata = await provider.get_track_metadata(track)

    assert metadata is not None
    assert metadata.lrc_lyrics == "[00:01.00]sync"
    provider._get_data.assert_awaited_once_with(
        track_name="Test Track", artist_name="Test Artist", album_name="", duration=200
    )


async def test_get_track_metadata_skips_when_lrc_lyrics_exist(provider: LrclibProvider) -> None:
    """Existing synchronized lyrics do not trigger another lookup."""
    track = create_track("spotify", "track")
    track.metadata.lrc_lyrics = "[00:01.00]existing"
    provider._get_data = AsyncMock(  # type: ignore[method-assign]
        return_value={"syncedLyrics": "[00:02.00]later"}
    )

    assert await provider.get_track_metadata(track) is None
    provider._get_data.assert_not_awaited()


async def test_get_track_metadata_returns_plain_lyrics_without_lrc(
    provider: LrclibProvider,
) -> None:
    """Plain lyrics returned by LRCLIB remain available when no synchronized lyrics exist."""
    track = create_track("spotify", "track")
    provider._get_data = AsyncMock(  # type: ignore[method-assign]
        return_value={"plainLyrics": "plain lyrics"}
    )

    metadata = await provider.get_track_metadata(track)

    assert metadata is not None
    assert metadata.lyrics == "plain lyrics"
    assert metadata.lrc_lyrics is None


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


async def test_get_data_rate_limited_honours_retry_after(provider: LrclibProvider) -> None:
    """A 429 arms the shared cooldown for the Retry-After duration."""
    response = MagicMock()
    response.status = 429
    response.headers = {"Retry-After": "120"}
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_response_cm(response=response)
    )

    with pytest.raises(MusicAssistantError):
        await provider._get_data(track_name="Song", artist_name="Artist")
    assert provider.throttler.cooldown_remaining == pytest.approx(120, abs=1)


@pytest.mark.parametrize(
    ("exc", "cause"),
    [(ClientError("network down"), "network down"), (TimeoutError(), "TimeoutError")],
)
async def test_get_data_failure_logs_cause(
    provider: LrclibProvider, caplog: pytest.LogCaptureFixture, exc: Exception, cause: str
) -> None:
    """The retry log names the underlying error instead of a bare 'request failed'."""
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_response_cm(exc=exc)
    )

    with caplog.at_level(logging.INFO), pytest.raises(MusicAssistantError):
        await provider._get_data(track_name="Song", artist_name="Artist")
    assert f"LRCLIB request failed: {cause}" in caplog.text
