"""Test the episode ordering of the Podcast RSS Feed provider."""

from typing import Any
from unittest.mock import Mock

from music_assistant.providers.podcastfeed import SUPPORTED_FEATURES, PodcastMusicprovider


def _episode(guid: str, published: int) -> dict[str, Any]:
    """Return a minimal podcastparser episode dict with a valid enclosure."""
    return {
        "guid": guid,
        "title": f"Episode {guid}",
        "published": published,
        "enclosures": [{"url": f"https://example.com/{guid}.mp3"}],
    }


def _provider(episodes: list[dict[str, Any]]) -> PodcastMusicprovider:
    """Return a provider serving the given episodes, bypassing the feed fetch."""
    mass = Mock()
    manifest = Mock(domain="podcastfeed")
    config = Mock(instance_id="podcastfeed--test", enabled=True)
    config.get_value.side_effect = lambda key, default=None: (
        "INFO" if key == "log_level" else default
    )
    provider = PodcastMusicprovider(mass, manifest, config, SUPPORTED_FEATURES)
    provider.feed_url = "https://example.com/feed.xml"
    provider.podcast_id = "testpodcast"
    provider.parsed_podcast = {"title": "My Show", "episodes": episodes}
    return provider


async def test_episodes_are_yielded_newest_first() -> None:
    """The newest episode comes first, so callers after the latest one take the first."""
    provider = _provider([_episode("b", 200), _episode("c", 300), _episode("a", 100)])
    episodes = [ep async for ep in provider.get_podcast_episodes("testpodcast")]
    assert [ep.item_id for ep in episodes] == ["c", "b", "a"]


async def test_episode_positions_run_oldest_to_newest() -> None:
    """The newest episode still carries the highest position."""
    provider = _provider([_episode("b", 200), _episode("c", 300), _episode("a", 100)])
    episodes = [ep async for ep in provider.get_podcast_episodes("testpodcast")]
    assert {ep.item_id: ep.position for ep in episodes} == {"a": 1, "b": 2, "c": 3}
