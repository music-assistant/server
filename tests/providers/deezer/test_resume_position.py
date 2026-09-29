"""Test that Deezer resume positions carry the time Deezer saved them."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import MediaType

from music_assistant.providers.deezer.provider import DeezerProvider
from music_assistant.providers.deezer.streaming import DeezerStreamingManager


def _bookmarks(*nodes: SimpleNamespace) -> SimpleNamespace:
    """Return one page of podcast episode bookmarks as the GraphQL client parses it."""
    page_info = SimpleNamespace(has_next_page=False, end_cursor=None)
    edges = [SimpleNamespace(node=node) for node in nodes]
    return SimpleNamespace(
        podcast_episode_bookmarks=SimpleNamespace(edges=edges, page_info=page_info)
    )


def _bookmark(episode_id: str, bookmarked_at: str) -> SimpleNamespace:
    """Return a bookmark 30 seconds into an episode that is not played yet."""
    return SimpleNamespace(
        episode=SimpleNamespace(id=episode_id),
        is_played=False,
        position=30,
        bookmarked_at=bookmarked_at,
    )


@pytest.fixture
def gql(provider: DeezerProvider) -> AsyncMock:
    """Stub the GraphQL client and wire up a real streaming manager."""
    client = AsyncMock()
    provider.gql_client = client
    provider.streaming_manager = DeezerStreamingManager(provider)
    return client


async def test_resume_position_carries_the_bookmark_time(
    provider: DeezerProvider, gql: AsyncMock
) -> None:
    """The time Deezer saved the bookmark comes along, so the newer position can win."""
    gql.get_podcast_episode_bookmarks.return_value = _bookmarks(
        _bookmark("100", "2026-09-10T06:14:42.000Z")
    )

    result = await provider.get_resume_position("100", MediaType.PODCAST_EPISODE)

    assert result == (False, 30000, datetime(2026, 9, 10, 6, 14, 42, tzinfo=UTC))


async def test_episode_without_bookmark(provider: DeezerProvider, gql: AsyncMock) -> None:
    """An episode Deezer has no bookmark for starts from the beginning."""
    gql.get_podcast_episode_bookmarks.return_value = _bookmarks(
        _bookmark("100", "2026-09-10T06:14:42.000Z")
    )

    result = await provider.get_resume_position("200", MediaType.PODCAST_EPISODE)

    assert result == (False, 0, None)


async def test_unreadable_bookmark_time_keeps_the_position(
    provider: DeezerProvider, gql: AsyncMock
) -> None:
    """A bookmark time that can not be read leaves the timestamp out, not the position."""
    gql.get_podcast_episode_bookmarks.return_value = _bookmarks(_bookmark("100", ""))

    result = await provider.get_resume_position("100", MediaType.PODCAST_EPISODE)

    assert result == (False, 30000, None)


async def test_only_podcast_episodes_are_looked_up(
    provider: DeezerProvider, gql: AsyncMock
) -> None:
    """Deezer keeps bookmarks for podcast episodes only."""
    result = await provider.get_resume_position("100", MediaType.TRACK)

    assert result == (False, 0, None)
    gql.get_podcast_episode_bookmarks.assert_not_called()
