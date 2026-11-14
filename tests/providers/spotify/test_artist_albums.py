"""Tests for the Spotify artist albums listing."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.spotify import provider as spotify_provider
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

    monkeypatch.setattr(spotify_provider, "parse_album", lambda item, _prov: item["id"])
    return prov


def _serve_discography(provider: SpotifyProvider, total: int) -> AsyncMock:
    """Serve an artist discography of the given size and return the page fetch mock."""
    provider._get_cached_paginated_meta = AsyncMock(  # type: ignore[method-assign]
        return_value={"etag": "etag", "total": total}
    )

    async def _page(*_args: Any, offset: int, limit: int, **_kwargs: Any) -> dict[str, Any]:
        end = min(offset + limit, total)
        return {"items": [{"id": f"a{i}"} for i in range(offset, end)], "total": total}

    fetch_page = AsyncMock(side_effect=_page)
    provider._get_data_with_caching = fetch_page  # type: ignore[method-assign]
    return fetch_page


async def test_artist_albums_stop_at_the_cap(provider: SpotifyProvider) -> None:
    """A large discography is listed up to the cap without requesting further pages."""
    fetch_page = _serve_discography(provider, total=1000)

    albums = await provider.get_artist_albums("artist1")

    assert albums == [f"a{i}" for i in range(100)]
    assert fetch_page.await_count == 10


async def test_artist_albums_below_the_cap_are_listed_in_full(provider: SpotifyProvider) -> None:
    """A discography smaller than the cap is listed completely."""
    fetch_page = _serve_discography(provider, total=25)

    albums = await provider.get_artist_albums("artist1")

    assert albums == [f"a{i}" for i in range(25)]
    assert fetch_page.await_count == 3
