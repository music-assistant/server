"""Tests for matching gPodder episode actions to the episodes of a feed."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.helpers.datetime import from_utc_timestamp
from music_assistant.providers.gpodder import FEED_REFRESH_CONCURRENCY, GPodder
from music_assistant.providers.gpodder.client import (
    EpisodeActionDelete,
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


def _subscribe(provider: GPodder, feeds: list[str]) -> None:
    cast("Mock", provider._client).get_subscriptions = AsyncMock(
        return_value=SubscriptionsGet(add=feeds, remove=[], timestamp=5)
    )


async def test_sync_writes_the_progress_of_every_matched_episode(provider: GPodder) -> None:
    """Episodes reported by url only are matched although their item id holds the guid."""
    _serve(provider)
    _subscribe(provider, [FEED])
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


async def test_sync_only_reads_what_is_new_for_known_feeds(provider: GPodder) -> None:
    """After a completed sync, the next one asks gPodder only for the actions since."""
    _serve(provider)
    # the server returns the whole history for since=0 and nothing newer than the last sync
    cast("Mock", provider._client).get_episode_actions = AsyncMock(
        side_effect=lambda since=0: (ACTIONS if since == 0 else [], 999)
    )
    _subscribe(provider, [FEED])
    music = cast("Mock", provider.mass.music)
    with patch(
        "music_assistant.providers.gpodder.refresh_cached_podcast",
        AsyncMock(return_value=PODCAST),
    ):
        _ = [podcast async for podcast in provider.get_library_podcasts()]
        assert music.mark_item_played.call_count == 1
        _ = [podcast async for podcast in provider.get_library_podcasts()]
        assert music.mark_item_played.call_count == 1
        # a newly subscribed feed brings in the whole history again
        _subscribe(provider, [FEED, "https://example.com/new.xml"])
        _ = [podcast async for podcast in provider.get_library_podcasts()]

    assert music.mark_item_played.call_count == 2


async def test_sync_refreshes_several_feeds_at_once(provider: GPodder) -> None:
    """Feeds are refreshed concurrently up to the limit, and a broken one is skipped."""
    _serve(provider)
    feeds = [f"https://example.com/{number}.xml" for number in range(7)]
    _subscribe(provider, feeds)
    running = peak = 0

    async def refresh(*, feed_url: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        if feed_url == feeds[3]:
            raise MediaNotFoundError("gone")
        return PODCAST

    with patch("music_assistant.providers.gpodder.refresh_cached_podcast", side_effect=refresh):
        podcasts = [podcast async for podcast in provider.get_library_podcasts()]

    assert sorted(podcast.item_id for podcast in podcasts) == sorted(set(feeds) - {feeds[3]})
    assert peak == FEED_REFRESH_CONCURRENCY


@pytest.mark.parametrize("synced", [True, False])
async def test_listing_shows_and_writes_what_is_new_since_the_sync(
    provider: GPodder, synced: bool
) -> None:
    """A synced feed fetches and writes only what is new, an unsynced one is read in full."""
    _serve(provider)
    if synced:
        provider.feeds = {FEED}

    episodes = [x async for x in provider.get_podcast_episodes(FEED)]

    assert [_progress(x) for x in episodes] == [
        ("guid-1", False, 600_000),
        ("guid-2", False, 0),
        ("guid-3", None, None),
    ]
    client = cast("Mock", provider._client)
    client.get_episode_actions.assert_awaited_once_with(since=100 if synced else 0)
    music = cast("Mock", provider.mass.music)
    played = music.mark_item_played
    assert [(c.args[0].item_id, c.kwargs["seconds_played"]) for c in played.call_args_list] == (
        [(f"{FEED} guid-1", 600)] if synced else []
    )
    unplayed = music.mark_item_unplayed
    assert [c.args[0].item_id for c in unplayed.call_args_list] == (
        [f"{FEED} guid-2"] if synced else []
    )
    # only the sync moves the timestamp, as only it writes every feed
    assert provider.timestamp_actions == 100


@pytest.mark.parametrize(
    ("action", "timestamp"),
    [
        # a mark as unplayed carries its time, so core prefers it over the playlog
        (EpisodeActionNew, from_utc_timestamp(999)),
        # a deleted download does not, so a finished episode stays finished
        (EpisodeActionDelete, None),
    ],
)
async def test_reset_in_another_client_wins_over_the_playlog(
    provider: GPodder, action: type[EpisodeActionNew | EpisodeActionDelete], timestamp: Any
) -> None:
    """Only an explicit reset since the last sync overrides MA's own resume position."""
    _serve(provider)
    reset = action(podcast=FEED, episode="https://example.com/ep2.mp3")
    cast("Mock", provider._client).get_episode_actions = AsyncMock(return_value=([reset], 999))

    resume = await provider.get_resume_position(f"{FEED} guid-2", MediaType.PODCAST_EPISODE)

    assert resume == (False, 0, timestamp)


# a feed without itunes:duration leaves the episode without one
@pytest.mark.parametrize("duration", [1200, 0])
async def test_on_played_uploads_without_reading_the_feed(provider: GPodder, duration: int) -> None:
    """The episode's own stream url identifies it, the cached feed is not needed."""
    _serve(provider)
    client = cast("Mock", provider._client)
    client.update_progress = AsyncMock()
    played = await anext(provider.get_podcast_episodes(FEED))
    played.duration = duration
    provider._cache_get_podcast = AsyncMock(side_effect=AssertionError("feed read"))  # type: ignore[method-assign]

    await provider.on_played(played.media_type, played.item_id, False, 30, played)

    assert client.update_progress.call_args.kwargs["episode_id"] == ("https://example.com/ep1.mp3")
