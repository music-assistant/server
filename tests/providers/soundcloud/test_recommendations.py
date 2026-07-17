"""Test Soundcloud recommendations() row filtering via the `wanted` parameter."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.soundcloud import SoundcloudMusicProvider

MIXED_DATA = {
    "collection": [
        {
            "id": "trending-music",
            "title": "Trending",
            "items": {
                "collection": [
                    {"kind": "system-playlist", "id": "sp1", "title": "Daily Mix"},
                ]
            },
        },
    ]
}
FEED_DATA = {
    "collection": [
        {
            "type": "track",
            "track": {
                "id": 111,
                "title": "Feed Track",
                "duration": 180000,
                "permalink_url": "https://soundcloud.com/artist/feed-track",
                "user": {"id": 42},
            },
        },
    ]
}
USER_DATA = {"id": 42, "username": "Artist", "permalink": "artist"}


def _stub_api(provider: SoundcloudMusicProvider) -> Mock:
    """Attach a stubbed soundcloud client with canned responses."""
    api = Mock()
    api.get_mixed_selection = AsyncMock(return_value=MIXED_DATA)
    api.get_subscribe_feed = AsyncMock(return_value=FEED_DATA)
    api.get_user_details = AsyncMock(return_value=USER_DATA)
    provider._soundcloud = api
    return api


def _install_cache_mocks(provider: SoundcloudMusicProvider) -> None:
    """Make the @use_cache decorator treat every call as a cache miss."""
    provider.mass.cache.get_with_freshness = AsyncMock(  # type: ignore[method-assign]
        return_value=(None, False, False)
    )
    provider.mass.cache.set = AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_recommendations_wanted_none_fetches_all_rows(
    provider: SoundcloudMusicProvider,
) -> None:
    """wanted=None (default) fetches mixed selections and the feed — unchanged behavior."""
    _install_cache_mocks(provider)
    api = _stub_api(provider)

    result = await provider.recommendations()

    api.get_mixed_selection.assert_awaited_once_with(40)
    api.get_subscribe_feed.assert_awaited_once_with(40)
    assert {f.item_id for f in result} == {
        f"{provider.instance_id}_trending-music",
        f"{provider.instance_id}_sc_subscribed_feed",
    }


@pytest.mark.asyncio
async def test_recommendations_wanted_feed_only_skips_mixed_selection(
    provider: SoundcloudMusicProvider,
) -> None:
    """wanted={<instance>_sc_subscribed_feed} only fetches the subscribed feed."""
    _install_cache_mocks(provider)
    api = _stub_api(provider)

    result = await provider.recommendations(wanted={f"{provider.instance_id}_sc_subscribed_feed"})

    api.get_mixed_selection.assert_not_awaited()
    api.get_subscribe_feed.assert_awaited_once_with(40)
    assert [f.item_id for f in result] == [f"{provider.instance_id}_sc_subscribed_feed"]


@pytest.mark.asyncio
async def test_recommendations_wanted_collection_only_skips_feed(
    provider: SoundcloudMusicProvider,
) -> None:
    """wanted={<instance>_trending-music} fetches the mixed selections but not the feed."""
    _install_cache_mocks(provider)
    api = _stub_api(provider)

    result = await provider.recommendations(wanted={f"{provider.instance_id}_trending-music"})

    api.get_mixed_selection.assert_awaited_once_with(40)
    api.get_subscribe_feed.assert_not_awaited()
    assert [f.item_id for f in result] == [f"{provider.instance_id}_trending-music"]
