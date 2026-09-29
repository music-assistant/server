"""Tests for the Spotify artist top tracks lookup."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.spotify.provider import SpotifyProvider


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> SpotifyProvider:
    """Return a SpotifyProvider with mocked mass/cache, bypassing __init__."""
    prov = object.__new__(SpotifyProvider)
    prov.config = MagicMock(instance_id="spotify--test")
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()

    mass = MagicMock()
    # bypass the use_cache decorator: always miss
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.create_task = MagicMock(side_effect=lambda coro, **_: asyncio.create_task(coro))
    prov.mass = mass

    monkeypatch.setattr(prov, "get_artist", AsyncMock())
    return prov


async def test_get_artist_toptracks_uses_global_session(
    provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Top tracks are fetched on the global session, as developer apps lack the endpoint."""
    get_data = AsyncMock(return_value={"tracks": []})
    monkeypatch.setattr(provider, "_get_data", get_data)

    await provider.get_artist_toptracks("artist1")

    get_data.assert_awaited_once_with("artists/artist1/top-tracks", use_global_session=True)


async def test_get_artist_toptracks_ignores_unchecksummed_cache(
    provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache lookup carries a checksum, so entries stored without one are not served."""
    monkeypatch.setattr(provider, "_get_data", AsyncMock(return_value={"tracks": []}))

    await provider.get_artist_toptracks("artist1")

    lookup = provider.mass.cache.get_with_freshness
    assert isinstance(lookup, AsyncMock)
    assert lookup.await_args is not None
    assert lookup.await_args.kwargs["checksum"]


async def test_get_artist_toptracks_handles_not_found(
    provider: SpotifyProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable endpoint results in an empty list."""
    monkeypatch.setattr(provider, "_get_data", AsyncMock(side_effect=MediaNotFoundError("nope")))

    assert await provider.get_artist_toptracks("artist1") == []
