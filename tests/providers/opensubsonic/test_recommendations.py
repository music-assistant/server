"""Test Open Subsonic recommendations() shelf filtering via the `wanted` parameter."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from music_assistant_models.media_items import RecommendationFolder

from music_assistant.providers.opensubsonic.sonic_provider import OpenSonicProvider


def _make_folder(provider: OpenSonicProvider, item_id: str, name: str) -> RecommendationFolder:
    return RecommendationFolder(item_id=item_id, provider=provider.domain, name=name)


def _stub_shelf_builders(provider: OpenSonicProvider) -> dict[str, AsyncMock]:
    """Replace all four shelf-builder methods with AsyncMocks and enable all config gates."""
    provider._enable_podcasts = True
    provider._show_faves = True
    provider._show_new = True
    provider._show_played = True

    mocks = {
        "_podcast_recommendations": AsyncMock(
            return_value=_make_folder(provider, "subsonic_newest_podcasts", "Newest Podcasts")
        ),
        "_favorites_recommendation": AsyncMock(
            return_value=_make_folder(provider, "subsonic_starred_albums", "Starred Albums")
        ),
        "_new_recommendations": AsyncMock(
            return_value=_make_folder(provider, "subsonic_new_albums", "New Albums")
        ),
        "_played_recommendations": AsyncMock(
            return_value=_make_folder(provider, "subsonic_most_played", "Most Played Albums")
        ),
    }
    for name, mock in mocks.items():
        setattr(provider, name, mock)
    return mocks


def _install_cache_mocks(provider: OpenSonicProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_recommendations_wanted_none_builds_all_shelves(
    provider: OpenSonicProvider,
) -> None:
    """wanted=None (default) with all config flags on builds and returns all four shelves."""
    _install_cache_mocks(provider)
    mocks = _stub_shelf_builders(provider)

    result = await provider.recommendations()

    for mock in mocks.values():
        mock.assert_awaited_once()
    assert {f.item_id for f in result} == {
        "subsonic_newest_podcasts",
        "subsonic_starred_albums",
        "subsonic_new_albums",
        "subsonic_most_played",
    }


@pytest.mark.asyncio
async def test_recommendations_wanted_new_albums_builds_only_that_shelf(
    provider: OpenSonicProvider,
) -> None:
    """wanted={subsonic_new_albums} runs only _new_recommendations and returns only that folder."""
    _install_cache_mocks(provider)
    mocks = _stub_shelf_builders(provider)

    result = await provider.recommendations(wanted={"subsonic_new_albums"})

    mocks["_new_recommendations"].assert_awaited_once()
    mocks["_podcast_recommendations"].assert_not_awaited()
    mocks["_favorites_recommendation"].assert_not_awaited()
    mocks["_played_recommendations"].assert_not_awaited()
    assert len(result) == 1
    assert result[0].item_id == "subsonic_new_albums"
