"""Tests for gpodder episode ids whose episode part holds a space."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType

from music_assistant.providers.gpodder import GPodder

from .conftest import FEED

STREAM_URL = "https://example.com/Episode54 Indy 1.mp3"


async def test_episode_with_a_space_in_guid_and_stream_url(provider: GPodder) -> None:
    """The id falls back to the stream url, which still resolves to the episode."""
    podcast = {
        "title": "Podcast",
        "episodes": [
            {
                "title": "Episode 54",
                "guid": "Episode54 Indy 1.mp3",
                "enclosures": [{"url": STREAM_URL, "mime_type": "audio/mpeg"}],
                "published": 1_700_000_000,
                "total_time": 1200,
            }
        ],
    }
    provider._cache_get_podcast = AsyncMock(return_value=podcast)  # type: ignore[method-assign]
    cast("Mock", provider._client).get_episode_actions = AsyncMock(return_value=([], 999))

    listed = await anext(provider.get_podcast_episodes(FEED))
    episode = await provider.get_podcast_episode(listed.item_id)
    details = await provider.get_stream_details(listed.item_id, MediaType.PODCAST_EPISODE)

    assert listed.item_id == f"{FEED} {STREAM_URL}"
    assert episode.item_id == listed.item_id
    assert details.path == STREAM_URL
