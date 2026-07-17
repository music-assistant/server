"""Test nicovideo recommendations() row filtering via the `wanted` parameter."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.providers.nicovideo import SUPPORTED_FEATURES
from music_assistant.providers.nicovideo.provider import NicovideoMusicProvider


@pytest.fixture
def provider() -> NicovideoMusicProvider:
    """Create a real NicovideoMusicProvider with mocked dependencies."""
    mass = Mock()
    manifest = Mock()
    manifest.domain = "nicovideo"
    config = Mock()
    config.instance_id = "nicovideo--test123"
    config.name = "Nicovideo Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "GLOBAL",
    }.get(key, default)
    return NicovideoMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


def _track(item_id: str) -> Track:
    """Create a minimal Track for stubbed user service responses."""
    return Track(
        item_id=item_id,
        provider="nicovideo--test123",
        name=f"Track {item_id}",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="nicovideo",
                provider_instance="nicovideo--test123",
            )
        },
    )


def _stub_user_service(provider: NicovideoMusicProvider) -> dict[str, AsyncMock]:
    """Replace the user service fetch methods with AsyncMocks returning canned tracks."""
    user = provider.service_manager.user
    mocks = {
        "get_recommendations": AsyncMock(return_value=[_track("rec1")]),
        "get_user_history": AsyncMock(return_value=[_track("hist1")]),
        "get_following_activities": AsyncMock(return_value=[_track("follow1")]),
        "get_like_history": AsyncMock(return_value=[_track("like1")]),
    }
    user.get_recommendations = mocks["get_recommendations"]  # type: ignore[method-assign]
    user.get_user_history = mocks["get_user_history"]  # type: ignore[method-assign]
    user.get_following_activities = mocks["get_following_activities"]  # type: ignore[method-assign]
    user.get_like_history = mocks["get_like_history"]  # type: ignore[method-assign]
    return mocks


def _install_cache_mocks(provider: NicovideoMusicProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_recommendations_wanted_none_fetches_all_rows(
    provider: NicovideoMusicProvider,
) -> None:
    """wanted=None (default) fetches and builds all four rows — unchanged behavior."""
    _install_cache_mocks(provider)
    mocks = _stub_user_service(provider)

    result = await provider.recommendations()

    mocks["get_recommendations"].assert_awaited_once_with(
        "video_recommendation_recommend", limit=25
    )
    mocks["get_user_history"].assert_awaited_once_with(limit=50)
    mocks["get_following_activities"].assert_awaited_once_with(limit=30)
    mocks["get_like_history"].assert_awaited_once_with(limit=50)
    assert {f.item_id for f in result} == {
        "nicovideo_recommendations",
        "nicovideo_history",
        "nicovideo_following_activities",
        "nicovideo_like_history",
    }


@pytest.mark.asyncio
async def test_recommendations_wanted_history_only_fetches_history(
    provider: NicovideoMusicProvider,
) -> None:
    """wanted={nicovideo_history} issues only the history fetch and returns only that row."""
    _install_cache_mocks(provider)
    mocks = _stub_user_service(provider)

    result = await provider.recommendations(wanted={"nicovideo_history"})

    mocks["get_user_history"].assert_awaited_once_with(limit=50)
    mocks["get_recommendations"].assert_not_awaited()
    mocks["get_following_activities"].assert_not_awaited()
    mocks["get_like_history"].assert_not_awaited()
    assert len(result) == 1
    assert result[0].item_id == "nicovideo_history"
