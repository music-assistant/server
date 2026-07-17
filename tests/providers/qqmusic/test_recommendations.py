"""Test QQ Music recommendations() row filtering via the `wanted` parameter."""

# mypy: ignore-errors

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

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


@pytest.mark.asyncio
async def test_recommendations_wanted_none_fetches_all_rows() -> None:
    """wanted=None (default) fetches and builds all three rows — unchanged behavior."""
    provider = _make_provider()

    result = await provider.recommendations()

    provider._qq_recommend.get_guess_recommend.assert_awaited_once()
    provider._qq_recommend.get_radar_recommend.assert_not_awaited()
    provider._qq_recommend.get_recommend_newsong.assert_awaited_once()
    provider._qq_recommend.get_recommend_songlist.assert_awaited_once()
    assert [folder.item_id for folder in result] == [
        "guess_recommend",
        "new_songs",
        "recommended_playlists",
    ]


@pytest.mark.asyncio
async def test_recommendations_wanted_new_songs_only_fetches_new_songs() -> None:
    """wanted={new_songs} issues only the newsong fetch and returns only that row."""
    provider = _make_provider()

    result = await provider.recommendations(wanted={"new_songs"})

    provider._qq_recommend.get_guess_recommend.assert_not_awaited()
    provider._qq_recommend.get_radar_recommend.assert_not_awaited()
    provider._qq_recommend.get_recommend_songlist.assert_not_awaited()
    provider._qq_recommend.get_recommend_newsong.assert_awaited_once()
    assert len(result) == 1
    assert result[0].item_id == "new_songs"


@pytest.mark.asyncio
async def test_recommendations_wanted_guess_gates_radar_fallback_together() -> None:
    """wanted={guess_recommend} with empty guess payload still runs the radar fallback."""
    provider = _make_provider()
    provider._qq_recommend.get_guess_recommend.return_value = {"songs": []}

    result = await provider.recommendations(wanted={"guess_recommend"})

    provider._qq_recommend.get_guess_recommend.assert_awaited_once()
    provider._qq_recommend.get_radar_recommend.assert_awaited_once()
    provider._qq_recommend.get_recommend_newsong.assert_not_awaited()
    provider._qq_recommend.get_recommend_songlist.assert_not_awaited()
    assert len(result) == 1
    assert result[0].item_id == "guess_recommend"
