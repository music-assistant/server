"""Test ZvukMusicProvider recommendations() row filtering via the `wanted` parameter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import Playlist

from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider


def _make_playlist(item_id: str = "1") -> Playlist:
    """Return a minimal Playlist mock."""
    pl = Mock(spec=Playlist)
    pl.item_id = item_id
    return pl


def _make_provider() -> Any:
    """Create a ZvukMusicProvider mock with helpers stubbed and recommendations bound."""
    provider = Mock(spec=ZvukMusicProvider)
    provider.instance_id = "zvuk_music"
    provider._get_for_you_playlists = AsyncMock(return_value=[_make_playlist("3")])
    provider._get_editorial_playlists = AsyncMock(return_value=[_make_playlist("99")])
    provider.recommendations = ZvukMusicProvider.recommendations.__get__(
        provider, ZvukMusicProvider
    )
    return provider


@pytest.mark.asyncio
async def test_recommendations_wanted_none_fetches_all_rows() -> None:
    """wanted=None (default) fetches and builds both rows — unchanged behavior."""
    provider = _make_provider()

    result = await provider.recommendations()

    provider._get_for_you_playlists.assert_awaited_once()
    provider._get_editorial_playlists.assert_awaited_once()
    assert [f.item_id for f in result] == ["for_you", "editorial"]


@pytest.mark.asyncio
async def test_recommendations_wanted_for_you_only_fetches_for_you() -> None:
    """wanted={"for_you"} fetches only the for-you playlists and returns only that folder."""
    provider = _make_provider()

    result = await provider.recommendations(wanted={"for_you"})

    provider._get_for_you_playlists.assert_awaited_once()
    provider._get_editorial_playlists.assert_not_awaited()
    assert [f.item_id for f in result] == ["for_you"]


@pytest.mark.asyncio
async def test_recommendations_wanted_editorial_only_fetches_editorial() -> None:
    """wanted={"editorial"} fetches only the editorial playlists and returns only that folder."""
    provider = _make_provider()

    result = await provider.recommendations(wanted={"editorial"})

    provider._get_for_you_playlists.assert_not_awaited()
    provider._get_editorial_playlists.assert_awaited_once()
    assert [f.item_id for f in result] == ["editorial"]
