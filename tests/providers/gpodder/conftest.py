"""Fixtures for the gPodder provider tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from music_assistant.providers.gpodder import SUPPORTED_FEATURES, GPodder

FEED = "https://example.com/feed.xml"


def episode(number: int, guid: str | None = None, **extra: Any) -> dict[str, Any]:
    """Build a raw episode as podcastparser returns it."""
    return {
        "title": f"Episode {number}",
        "guid": guid,
        "enclosures": [{"url": f"https://example.com/ep{number}.mp3", "mime_type": "audio/mpeg"}],
        "published": 1_700_000_000 + number,
        "total_time": 1200,
        **extra,
    }


@pytest.fixture
def provider() -> GPodder:
    """Return a gPodder provider whose server and cached feeds are stubbed."""
    mass = MagicMock()
    mass.music.mark_item_played = AsyncMock()
    mass.music.mark_item_unplayed = AsyncMock()
    mass.cache.set = AsyncMock()
    config = Mock(instance_id="gpodder--test")
    config.get_value.side_effect = lambda key, default=None: {"log_level": "INFO"}.get(key, default)
    provider = GPodder(mass, Mock(domain="gpodder"), config, SUPPORTED_FEATURES)
    provider._client = Mock()
    provider.feeds = set()
    provider.max_episodes = 0
    provider.timestamp_subscriptions = 0
    provider.timestamp_actions = 100
    provider.progress_guard_timestamp = 0.0
    return provider
