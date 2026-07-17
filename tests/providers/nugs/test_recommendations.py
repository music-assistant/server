"""Test Nugs.net recommendations() row filtering via the `wanted` parameter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from music_assistant.providers.nugs import NugsProvider

POPULAR_DATA = {"items": [{"id": "100"}]}
SHOW_DATA = {
    "Response": {"id": "100", "title": "Popular Show", "artist": {"id": "9", "name": "Artist"}}
}
RECOMMENDED_DATA = {
    "items": [{"id": "200", "title": "Recommended Show", "artist": {"id": "9", "name": "Artist"}}]
}
RECENT_DATA = {
    "items": [{"id": "300", "title": "Recent Show", "artist": {"id": "9", "name": "Artist"}}]
}


def _stub_get_data(provider: NugsProvider) -> AsyncMock:
    """Attach a _get_data stub that returns canned data keyed by endpoint."""

    async def _fake(_nugs_api: str, endpoint: str, **_kwargs: Any) -> Any:
        if endpoint == "releases/popular":
            return POPULAR_DATA
        if endpoint.startswith("shows/"):
            return SHOW_DATA
        if endpoint == "me/releases/recommendations":
            return RECOMMENDED_DATA
        if endpoint == "releases/recent":
            return RECENT_DATA
        return {"items": []}

    mock = AsyncMock(side_effect=_fake)
    provider._get_data = mock  # type: ignore[method-assign]
    return mock


def _install_cache_mocks(provider: NugsProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_recommendations_wanted_none_fetches_all_rows(provider: NugsProvider) -> None:
    """wanted=None (default) fetches and builds all three rows — unchanged behavior."""
    _install_cache_mocks(provider)
    api_mock = _stub_get_data(provider)

    result = await provider.recommendations()

    called_endpoints = {call.args[1] for call in api_mock.call_args_list}
    assert called_endpoints == {
        "releases/popular",
        "shows/100",
        "me/releases/recommendations",
        "releases/recent",
    }
    assert [f.item_id for f in result] == [
        "nugs_popular_shows",
        "nugs_recommended_shows",
        "nugs_recent_shows",
    ]


@pytest.mark.asyncio
async def test_recommendations_wanted_recent_only_fetches_recent(provider: NugsProvider) -> None:
    """wanted={nugs_recent_shows} fetches only releases/recent, skipping the popular N+1 loop."""
    _install_cache_mocks(provider)
    api_mock = _stub_get_data(provider)

    result = await provider.recommendations(wanted={"nugs_recent_shows"})

    api_mock.assert_awaited_once()
    assert api_mock.call_args.args == ("catalog", "releases/recent")
    assert len(result) == 1
    assert result[0].item_id == "nugs_recent_shows"
