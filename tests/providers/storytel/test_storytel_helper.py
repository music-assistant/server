"""Tests for Storytel podcast episode parsing."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from music_assistant.providers.storytel.storytel_helper import StorytelHelper


def _episode_data(**overrides: Any) -> dict[str, Any]:
    """Return minimal Storytel podcast episode API data."""
    data: dict[str, Any] = {
        "consumableId": "episode-1",
        "title": "Episode 1",
        "duration": {"hours": 0, "minutes": 30, "seconds": 0},
        "seriesInfo": {"id": "podcast-1", "orderInSeries": 14},
    }
    data.update(overrides)
    return data


@pytest.fixture
def helper() -> StorytelHelper:
    """Return a StorytelHelper whose provider_instance.get_podcast resolves to a stub podcast."""
    provider_instance = MagicMock()
    provider_instance.get_podcast = AsyncMock(return_value=MagicMock())
    return StorytelHelper(
        session=MagicMock(),
        provider_instance=provider_instance,
        provider_id="storytel--test",
        provider_domain="storytel",
    )


async def test_episode_number_is_set_from_order_in_series(helper: StorytelHelper) -> None:
    """OrderInSeries becomes episode_number, while position keeps its own sort-order value."""
    episode = await helper._parse_podcast_episode_item(_episode_data())
    assert episode.episode_number == 14
    assert episode.position == 14


async def test_missing_order_in_series_leaves_episode_number_unset(
    helper: StorytelHelper,
) -> None:
    """Without orderInSeries, episode_number stays None and position falls back to 0."""
    data = _episode_data()
    data["seriesInfo"] = {"id": "podcast-1"}
    episode = await helper._parse_podcast_episode_item(data)
    assert episode.episode_number is None
    assert episode.position == 0


async def test_zero_order_in_series_does_not_show_as_episode_zero(
    helper: StorytelHelper,
) -> None:
    """A seriesInfo.orderInSeries of 0 is not a real episode number, so it is not displayed."""
    data = _episode_data()
    data["seriesInfo"] = {"id": "podcast-1", "orderInSeries": 0}
    episode = await helper._parse_podcast_episode_item(data)
    assert episode.episode_number is None
