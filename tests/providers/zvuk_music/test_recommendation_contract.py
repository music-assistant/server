"""Provider-side regression tests for the recommendation result contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.media_items import UniqueList

from music_assistant.providers.zvuk_music.provider import ZvukMusicProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("item_id", ["for_you", "editorial"])
async def test_successful_recommendation_rows_return_unique_lists(item_id: str) -> None:
    """Successful recommendation rows preserve the ``UniqueList`` contract."""
    provider = Mock(spec=ZvukMusicProvider)
    provider._get_for_you_playlists = AsyncMock(return_value=[])
    provider._get_editorial_playlists = AsyncMock(return_value=[])
    provider.get_recommendation_items = ZvukMusicProvider.get_recommendation_items.__get__(
        provider, ZvukMusicProvider
    )

    result = await provider.get_recommendation_items(item_id)

    assert isinstance(result, UniqueList)
