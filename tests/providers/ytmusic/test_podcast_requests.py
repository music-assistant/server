"""Test how often YouTube Music is asked for a podcast when its page or an episode opens."""

import asyncio
import copy
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.ytmusic import YoutubeMusicProvider
from tests.common import use_real_create_task

PODCAST_ID = "PLpodcast"


class _MemoryCache:
    """Cache that keeps what `@use_cache` stores, so a second lookup hits."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], Any] = {}

    async def get_with_freshness(
        self, key: str, provider: str, **kwargs: Any
    ) -> tuple[Any, bool, bool]:
        if (provider, key) not in self._data:
            return None, False, False
        return copy.deepcopy(self._data[(provider, key)]), True, True

    async def set(self, key: str, data: Any, provider: str, **kwargs: Any) -> None:
        self._data[(provider, key)] = data.to_dict() if hasattr(data, "to_dict") else data


@pytest.fixture
def provider() -> YoutubeMusicProvider:
    """Return a YoutubeMusicProvider instance with an empty in-memory cache."""
    mass = AsyncMock()
    mass.cache = _MemoryCache()
    use_real_create_task(mass)
    manifest = MagicMock()
    manifest.domain = "ytmusic"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    prov = YoutubeMusicProvider(mass, manifest, config)
    prov._headers = {}
    prov._yt_user = None
    prov.language = "en"
    return prov


@pytest.fixture
def podcast_helpers() -> Generator[tuple[AsyncMock, AsyncMock]]:
    """Patch the podcast and episode helpers that each cost a request to YouTube Music."""
    podcast_obj = {
        "podcastId": PODCAST_ID,
        "title": "Some Podcast",
        "author": {"name": "Some Channel", "id": "UCchannel"},
        "episodes": [
            {"videoId": "ep2", "title": "Episode 2", "duration": "25 min"},
            {"videoId": "ep1", "title": "Episode 1", "duration": "30 min"},
        ],
    }
    episode_obj = {"videoId": "ep1", "title": "Episode 1", "duration": "30 min"}
    with (
        patch(
            "music_assistant.providers.ytmusic.get_podcast",
            AsyncMock(side_effect=lambda *_args, **_kwargs: copy.deepcopy(podcast_obj)),
        ) as get_podcast,
        patch(
            "music_assistant.providers.ytmusic.get_podcast_episode",
            AsyncMock(return_value=episode_obj),
        ) as get_podcast_episode,
    ):
        yield get_podcast, get_podcast_episode


async def _drain_tasks() -> None:
    """Let the background tasks that store the cache entries run."""
    for _ in range(5):
        await asyncio.sleep(0)


async def _list_episodes(provider: YoutubeMusicProvider) -> list[str]:
    return [episode.item_id async for episode in provider.get_podcast_episodes(PODCAST_ID)]


async def test_page_view_fetches_podcast_once(
    provider: YoutubeMusicProvider, podcast_helpers: tuple[AsyncMock, AsyncMock]
) -> None:
    """Opening a podcast page fetches the podcast and lists its episodes with one request."""
    get_podcast, _ = podcast_helpers

    podcast = await provider.get_podcast(PODCAST_ID)
    await _drain_tasks()
    episode_ids = await _list_episodes(provider)

    assert get_podcast.await_count == 1
    assert podcast.name == "Some Podcast"
    assert episode_ids == [f"{PODCAST_ID}|ep2", f"{PODCAST_ID}|ep1"]


async def test_concurrent_page_view_fetches_podcast_once(
    provider: YoutubeMusicProvider, podcast_helpers: tuple[AsyncMock, AsyncMock]
) -> None:
    """The podcast and its episodes requested together share one request."""
    get_podcast, _ = podcast_helpers

    await asyncio.gather(provider.get_podcast(PODCAST_ID), _list_episodes(provider))

    assert get_podcast.await_count == 1


async def test_episode_fetches_podcast_and_episode_once(
    provider: YoutubeMusicProvider, podcast_helpers: tuple[AsyncMock, AsyncMock]
) -> None:
    """Opening an episode costs one request for its podcast and one for the episode."""
    get_podcast, get_podcast_episode = podcast_helpers

    episode = await provider.get_podcast_episode(f"{PODCAST_ID}|ep1")

    assert get_podcast.await_count == 1
    assert get_podcast_episode.await_count == 1
    assert episode.podcast.name == "Some Podcast"
