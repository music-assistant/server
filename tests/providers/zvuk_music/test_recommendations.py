"""Test ZvukMusicProvider's two-method recommendations contract."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import Playlist, UniqueList

from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider


def _make_playlist(item_id: str = "1") -> Playlist:
    """Return a minimal Playlist mock."""
    pl = Mock(spec=Playlist)
    pl.item_id = item_id
    return pl


def _make_provider() -> Any:
    """Create a ZvukMusicProvider mock with helpers stubbed and the real methods bound."""
    provider = Mock(spec=ZvukMusicProvider)
    provider.instance_id = "zvuk_music"
    provider._get_for_you_playlists = AsyncMock(return_value=[_make_playlist("3")])
    provider._get_editorial_playlists = AsyncMock(return_value=[_make_playlist("99")])
    provider.get_recommendations = ZvukMusicProvider.get_recommendations.__get__(
        provider, ZvukMusicProvider
    )
    provider.get_recommendation_items = ZvukMusicProvider.get_recommendation_items.__get__(
        provider, ZvukMusicProvider
    )
    return provider


@pytest.mark.asyncio
async def test_get_recommendations_returns_static_rows_without_backend_calls() -> None:
    """get_recommendations returns both row descriptors without any backend fetch."""
    provider = _make_provider()

    result = await provider.get_recommendations()

    provider._get_for_you_playlists.assert_not_awaited()
    provider._get_editorial_playlists.assert_not_awaited()
    assert [f.item_id for f in result] == ["for_you", "editorial"]
    assert all(f.provider == "zvuk_music" for f in result)
    assert all(len(f.items) == 0 for f in result)
    for_you, editorial = result
    assert for_you.name == "Made for you"
    assert for_you.translation_key == "made_for_you"
    assert for_you.icon == "mdi-playlist-music"
    assert editorial.name == "Collections"
    assert editorial.translation_key == "editorial"
    assert editorial.icon == "mdi-music-box-multiple"
    assert editorial.subtitle == "Editorial playlists from Zvuk by genre"


@pytest.mark.asyncio
async def test_get_recommendation_items_for_you_fetches_only_for_you() -> None:
    """The for_you row triggers only the for-you fetch and returns its playlists."""
    provider = _make_provider()

    result = await provider.get_recommendation_items("for_you")

    provider._get_for_you_playlists.assert_awaited_once()
    provider._get_editorial_playlists.assert_not_awaited()
    assert [item.item_id for item in result] == ["3"]


@pytest.mark.asyncio
async def test_get_recommendation_items_editorial_fetches_only_editorial() -> None:
    """The editorial row triggers only the editorial fetch and returns its playlists."""
    provider = _make_provider()

    result = await provider.get_recommendation_items("editorial")

    provider._get_for_you_playlists.assert_not_awaited()
    provider._get_editorial_playlists.assert_awaited_once()
    assert [item.item_id for item in result] == ["99"]


@pytest.mark.asyncio
async def test_get_recommendation_items_unknown_id_returns_empty() -> None:
    """An unknown row item_id returns an empty UniqueList without any backend fetch."""
    provider = _make_provider()

    result = await provider.get_recommendation_items("bogus")

    provider._get_for_you_playlists.assert_not_awaited()
    provider._get_editorial_playlists.assert_not_awaited()
    assert isinstance(result, UniqueList)
    assert len(result) == 0
