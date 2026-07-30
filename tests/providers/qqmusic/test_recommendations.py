"""Test QQ Music two-method recommendations: static rows + per-row item fetches."""

# mypy: ignore-errors

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.qqmusic import QQMusicProvider

GUESS_PAYLOAD = {
    "songs": [{"mid": "t1", "title": "Guess Song", "singer": [{"mid": "a1", "name": "Artist One"}]}]
}
RADAR_PAYLOAD = {
    "songs": [{"mid": "t2", "title": "Radar Song", "singer": [{"mid": "a2", "name": "Artist Two"}]}]
}
NEWSONG_PAYLOAD = {
    "songs": [{"mid": "t3", "title": "New Song", "singer": [{"mid": "a3", "name": "Artist Three"}]}]
}
SONGLIST_PAYLOAD = {"songlists": [{"id": 123, "title": "Recommended List"}]}


def _make_provider() -> QQMusicProvider:
    """Create a QQMusicProvider with stubbed recommendation backend calls."""
    provider = QQMusicProvider.__new__(QQMusicProvider)
    provider.manifest = SimpleNamespace(domain="qqmusic")
    provider.config = SimpleNamespace(instance_id="qqmusic_instance")
    provider.logger = Mock()
    provider._credential = Mock()
    provider._recommend_payload_cache = {}
    provider._qq_recommend = SimpleNamespace(
        get_guess_recommend=AsyncMock(return_value=GUESS_PAYLOAD),
        get_radar_recommend=AsyncMock(return_value=RADAR_PAYLOAD),
        get_recommend_newsong=AsyncMock(return_value=NEWSONG_PAYLOAD),
        get_recommend_songlist=AsyncMock(return_value=SONGLIST_PAYLOAD),
    )

    async def _run_with_session(coro):
        return await coro

    provider._run_with_session = _run_with_session
    return provider


def _assert_no_backend_calls(provider: QQMusicProvider) -> None:
    """Assert none of the stubbed recommendation endpoints were awaited."""
    provider._qq_recommend.get_guess_recommend.assert_not_awaited()
    provider._qq_recommend.get_radar_recommend.assert_not_awaited()
    provider._qq_recommend.get_recommend_newsong.assert_not_awaited()
    provider._qq_recommend.get_recommend_songlist.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_recommendations_static_rows_zero_backend_calls() -> None:
    """get_recommendations returns the three static rows without any backend I/O."""
    provider = _make_provider()

    rows = await provider.get_recommendations()

    _assert_no_backend_calls(provider)
    assert [row.item_id for row in rows] == [
        "guess_recommend",
        "new_songs",
        "recommended_playlists",
    ]
    assert all(row.provider == "qqmusic_instance" for row in rows)
    assert all(len(row.items) == 0 for row in rows)
    assert [row.translation_key for row in rows] == [
        "recommended_tracks",
        "recommended_new_tracks",
        "recommended_playlists",
    ]
    assert [row.icon for row in rows] == [
        "mdi-lightbulb-on-outline",
        "mdi-music-note-plus",
        "mdi-playlist-music",
    ]


@pytest.mark.asyncio
async def test_get_recommendation_items_guess_recommend() -> None:
    """guess_recommend triggers only the guess fetch and returns its tracks."""
    provider = _make_provider()

    items = await provider.get_recommendation_items("guess_recommend")

    provider._qq_recommend.get_guess_recommend.assert_awaited_once()
    provider._qq_recommend.get_radar_recommend.assert_not_awaited()
    provider._qq_recommend.get_recommend_newsong.assert_not_awaited()
    provider._qq_recommend.get_recommend_songlist.assert_not_awaited()
    assert [item.item_id for item in items] == ["t1"]


@pytest.mark.asyncio
async def test_get_recommendation_items_guess_falls_back_to_radar() -> None:
    """An empty guess payload falls back to the radar fetch within the same row."""
    provider = _make_provider()
    provider._qq_recommend.get_guess_recommend.return_value = {"songs": []}

    items = await provider.get_recommendation_items("guess_recommend")

    provider._qq_recommend.get_guess_recommend.assert_awaited_once()
    provider._qq_recommend.get_radar_recommend.assert_awaited_once()
    provider._qq_recommend.get_recommend_newsong.assert_not_awaited()
    provider._qq_recommend.get_recommend_songlist.assert_not_awaited()
    assert [item.item_id for item in items] == ["t2"]


@pytest.mark.asyncio
async def test_get_recommendation_items_new_songs() -> None:
    """new_songs triggers only the newsong fetch and returns its tracks."""
    provider = _make_provider()

    items = await provider.get_recommendation_items("new_songs")

    provider._qq_recommend.get_recommend_newsong.assert_awaited_once()
    provider._qq_recommend.get_guess_recommend.assert_not_awaited()
    provider._qq_recommend.get_radar_recommend.assert_not_awaited()
    provider._qq_recommend.get_recommend_songlist.assert_not_awaited()
    assert [item.item_id for item in items] == ["t3"]


@pytest.mark.asyncio
async def test_get_recommendation_items_recommended_playlists() -> None:
    """recommended_playlists triggers only the songlist fetch and returns its playlists."""
    provider = _make_provider()

    items = await provider.get_recommendation_items("recommended_playlists")

    provider._qq_recommend.get_recommend_songlist.assert_awaited_once()
    provider._qq_recommend.get_guess_recommend.assert_not_awaited()
    provider._qq_recommend.get_radar_recommend.assert_not_awaited()
    provider._qq_recommend.get_recommend_newsong.assert_not_awaited()
    assert len(items) == 1
    assert items[0].name == "Recommended List"


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty() -> None:
    """An unknown row item_id returns an empty UniqueList without backend calls."""
    provider = _make_provider()

    items = await provider.get_recommendation_items("bogus_row")

    _assert_no_backend_calls(provider)
    assert isinstance(items, UniqueList)
    assert len(items) == 0


@pytest.mark.asyncio
async def test_get_recommendation_items_uses_payload_cache() -> None:
    """A repeated items call within the TTL is served from the payload cache."""
    provider = _make_provider()

    first = await provider.get_recommendation_items("new_songs")
    second = await provider.get_recommendation_items("new_songs")

    provider._qq_recommend.get_recommend_newsong.assert_awaited_once()
    assert [item.item_id for item in first] == [item.item_id for item in second] == ["t3"]
