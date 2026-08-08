"""Tests for Pocket Casts playback status handling."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.pocketcasts import PocketCastsProvider


@pytest.fixture
def client() -> AsyncMock:
    """Return a mocked Pocket Casts API client."""
    return AsyncMock()


@pytest.fixture
def provider(client: AsyncMock) -> PocketCastsProvider:
    """Return a PocketCastsProvider backed by the mocked API client."""
    manifest = MagicMock()
    manifest.domain = "pocketcasts"
    config = MagicMock()
    config.instance_id = "pocketcasts"
    config.get_value.return_value = None
    prov = PocketCastsProvider(MagicMock(), manifest, config)
    prov._client = client
    return prov


def _feed_episode(**overrides: Any) -> dict[str, Any]:
    """Build a full-podcast feed episode (snake_case schema, no playback status)."""
    return {
        "uuid": "episode-1",
        "title": "Episode 1",
        "url": "https://example.com/ep1.mp3",
        "file_type": "audio/mpeg",
        "duration": 1800,
        **overrides,
    }


async def test_sync_survives_episode_without_duration(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """A feed episode with a null duration must not abort the episode listing."""
    client.get_podcast_episodes.return_value = [
        _feed_episode(uuid="episode-1", duration=None),
        _feed_episode(uuid="episode-2", duration=1800),
    ]
    client.get_in_progress_episodes.return_value = []
    client.get_history.return_value = []

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert [episode.item_id for episode in episodes] == [
        "podcast-1:episode-1",
        "podcast-1:episode-2",
    ]
    assert episodes[0].duration == 0
    assert episodes[0].fully_played is False
    assert episodes[0].resume_position_ms == 0


async def test_sync_survives_null_status_fields(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """Null playedUpTo/duration on an in-progress entry must not abort the listing."""
    client.get_podcast_episodes.return_value = [_feed_episode(uuid="episode-1", duration=None)]
    client.get_in_progress_episodes.return_value = [
        {"uuid": "episode-1", "playedUpTo": None, "duration": None}
    ]
    client.get_history.return_value = []

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert len(episodes) == 1
    assert episodes[0].fully_played is False
    assert episodes[0].resume_position_ms == 0


async def test_sync_still_marks_played_episodes(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """An episode played past the threshold is still reported as fully played."""
    client.get_podcast_episodes.return_value = [_feed_episode(duration=1000)]
    client.get_in_progress_episodes.return_value = [
        {"uuid": "episode-1", "playedUpTo": 950, "duration": 1000}
    ]
    client.get_history.return_value = []

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert episodes[0].fully_played is True
    assert episodes[0].resume_position_ms == 0


async def test_sync_reports_resume_position(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """A partially played episode keeps its resume position."""
    client.get_podcast_episodes.return_value = [_feed_episode(duration=1000)]
    client.get_in_progress_episodes.return_value = [
        {"uuid": "episode-1", "playedUpTo": 300, "duration": 1000}
    ]
    client.get_history.return_value = []

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert episodes[0].fully_played is False
    assert episodes[0].resume_position_ms == 300000


async def test_get_podcast_episode_handles_null_fields(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """The single-episode endpoint must tolerate null duration/playedUpTo."""
    client.get_episode_details.return_value = {
        "uuid": "episode-1",
        "title": "Episode 1",
        "url": "https://example.com/ep1.mp3",
        "fileType": "audio/mpeg",
        "duration": None,
        "playedUpTo": None,
        "playingStatus": 1,
    }

    # call the undecorated function so the @use_cache wrapper stays out of the test
    get_podcast_episode = cast("Any", PocketCastsProvider.get_podcast_episode).__wrapped__
    episode = await get_podcast_episode(provider, "podcast-1:episode-1")

    assert episode.fully_played is False
    assert episode.resume_position_ms == 0


async def test_get_resume_position_handles_null_fields(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """A null playedUpTo/duration on an in-progress entry yields a zero resume point."""
    client.get_in_progress_episodes.return_value = [
        {"uuid": "episode-1", "playedUpTo": None, "duration": None}
    ]

    assert await provider.get_resume_position("podcast-1:episode-1", MediaType.PODCAST_EPISODE) == (
        False,
        0,
        None,
    )
