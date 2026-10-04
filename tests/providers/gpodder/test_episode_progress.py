"""Tests for matching gPodder episode actions to the episodes of a feed."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from music_assistant.providers.gpodder import GPodder
from music_assistant.providers.gpodder.client import (
    EpisodeActionNew,
    EpisodeActionPlay,
    SubscriptionsGet,
)

from .conftest import FEED, episode

PODCAST = {
    "title": "Podcast",
    "episodes": [
        episode(1, guid="guid-1"),
        episode(2, guid="guid-2"),
        episode(3, guid="guid-3"),
    ],
}
# newest first, as the client returns them; many clients report an episode by its url only
ACTIONS = [
    # the newer action of episode 2 names it by url, the older one by guid
    EpisodeActionNew(podcast=FEED, episode="https://example.com/ep2.mp3"),
    EpisodeActionPlay(podcast=FEED, episode="https://example.com/ep2.mp3", guid="guid-2"),
    EpisodeActionPlay(
        podcast=FEED, episode="https://example.com/ep1.mp3", position=600, total=1200
    ),
    EpisodeActionPlay(podcast="https://example.com/other.xml", episode="x", position=5, total=9),
]


def _serve(provider: GPodder) -> None:
    cast("Mock", provider._client).get_episode_actions = AsyncMock(return_value=(ACTIONS, 999))
    provider._cache_get_podcast = AsyncMock(return_value=PODCAST)  # type: ignore[method-assign]


def _progress(item: Any) -> tuple[str, bool | None, int | None]:
    return item.item_id.split(" ")[1], item.fully_played, item.resume_position_ms


async def test_sync_writes_the_progress_of_every_matched_episode(provider: GPodder) -> None:
    """Episodes reported by url only are matched although their item id holds the guid."""
    _serve(provider)
    cast("Mock", provider._client).get_subscriptions = AsyncMock(
        return_value=SubscriptionsGet(add=[FEED], remove=[], timestamp=5)
    )
    with patch(
        "music_assistant.providers.gpodder.refresh_cached_podcast",
        AsyncMock(return_value=PODCAST),
    ):
        podcasts = [podcast async for podcast in provider.get_library_podcasts()]

    assert [podcast.item_id for podcast in podcasts] == [FEED]
    music = cast("Mock", provider.mass.music)
    played = music.mark_item_played
    assert [(c.args[0].item_id, c.kwargs["seconds_played"]) for c in played.call_args_list] == [
        (f"{FEED} guid-1", 600)
    ]
    unplayed = music.mark_item_unplayed
    assert [c.args[0].item_id for c in unplayed.call_args_list] == [f"{FEED} guid-2"]
    assert provider.timestamp_actions == 999


async def test_listing_shows_the_progress_without_writing_it(provider: GPodder) -> None:
    """The listing carries the newest progress, leaving the playlog to the library sync."""
    _serve(provider)

    episodes = [x async for x in provider.get_podcast_episodes(FEED)]

    assert [_progress(x) for x in episodes] == [
        ("guid-1", False, 600_000),
        ("guid-2", False, 0),
        ("guid-3", None, None),
    ]
    music = cast("Mock", provider.mass.music)
    music.mark_item_played.assert_not_called()
    music.mark_item_unplayed.assert_not_called()
    # actions not written to the playlog stay visible to get_resume_position
    assert provider.timestamp_actions == 100


async def test_on_played_uploads_without_reading_the_feed(provider: GPodder) -> None:
    """The episode's own stream url identifies it, the cached feed is not needed."""
    _serve(provider)
    client = cast("Mock", provider._client)
    client.update_progress = AsyncMock()
    played = await anext(provider.get_podcast_episodes(FEED))
    provider._cache_get_podcast = AsyncMock(side_effect=AssertionError("feed read"))  # type: ignore[method-assign]

    await provider.on_played(played.media_type, played.item_id, False, 30, played)

    assert client.update_progress.call_args.kwargs["episode_id"] == ("https://example.com/ep1.mp3")
