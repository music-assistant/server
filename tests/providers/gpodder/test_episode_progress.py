"""Tests for matching gPodder episode actions to the episodes of a feed."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, call, patch

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
from music_assistant.providers.gpodder.helpers import index_actions

from .conftest import FEED, episode

PODCAST = {
    "title": "Podcast",
    "episodes": [
        episode(1, guid="guid-1"),
        episode(2, guid="guid-2"),
        episode(3, guid="guid-3"),
    ],
}
# many clients report an episode by its url only
ACTIONS = [
    # the newer action of episode 2 names it by url, the older one by guid
    EpisodeActionNew(
        podcast=FEED, episode="https://example.com/ep2.mp3", timestamp="2024-01-04T10:00:00"
    ),
    EpisodeActionPlay(
        podcast=FEED,
        episode="https://example.com/ep2.mp3",
        guid="guid-2",
        timestamp="2024-01-03T10:00:00",
    ),
    EpisodeActionPlay(
        podcast=FEED,
        episode="https://example.com/ep1.mp3",
        position=600,
        total=1200,
        timestamp="2024-01-02T10:00:00",
    ),
    EpisodeActionPlay(
        podcast="https://example.com/other.xml",
        episode="x",
        position=5,
        total=9,
        timestamp="2024-01-01T10:00:00",
    ),
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


def test_index_ranks_actions_newest_first_whatever_their_timestamps() -> None:
    """An unreadable or zone-qualified timestamp neither stops the ordering nor breaks it."""
    actions = [
        EpisodeActionPlay(podcast=FEED, episode="unreadable"),
        EpisodeActionPlay(podcast=FEED, episode="old", timestamp="2024-01-01T10:00:00"),
        EpisodeActionPlay(podcast=FEED, episode="newest", timestamp="2024-03-01T10:00:00+00:00"),
        EpisodeActionPlay(podcast=FEED, episode="middle", timestamp="2024-02-01T10:00:00"),
    ]

    ranked = sorted(index_actions(actions)[FEED].items(), key=lambda item: item[1][0])

    assert [key for key, _ in ranked] == ["newest", "middle", "old", "unreadable"]


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
    """Only a new feed brings in its whole history, also when its first refresh failed."""
    _serve(provider)
    new_feed = "https://example.com/new.xml"
    history = [
        *ACTIONS,
        EpisodeActionPlay(
            podcast=new_feed,
            episode="https://example.com/ep3.mp3",
            position=60,
            total=1200,
            timestamp="2024-01-05T10:00:00",
        ),
    ]
    # the server returns the whole history for since=0 and nothing newer than the last sync
    cast("Mock", provider._client).get_episode_actions = AsyncMock(
        side_effect=lambda since=0: (history if since == 0 else [], 999)
    )
    unreachable: set[str] = set()

    async def refresh(*, feed_url: str, **_kwargs: Any) -> dict[str, Any]:
        if feed_url in unreachable:
            raise MediaNotFoundError("unreachable")
        return PODCAST

    _subscribe(provider, [FEED])
    with patch("music_assistant.providers.gpodder.refresh_cached_podcast", side_effect=refresh):
        _ = [podcast async for podcast in provider.get_library_podcasts()]
        _ = [podcast async for podcast in provider.get_library_podcasts()]
        _subscribe(provider, [FEED, new_feed])
        unreachable.add(new_feed)
        _ = [podcast async for podcast in provider.get_library_podcasts()]
        unreachable.clear()
        _ = [podcast async for podcast in provider.get_library_podcasts()]

    played = cast("Mock", provider.mass.music).mark_item_played
    assert [c.args[0].item_id for c in played.call_args_list] == [
        f"{FEED} guid-1",
        f"{new_feed} guid-3",
    ]


@pytest.mark.parametrize(
    ("stored", "full_sync"),
    [
        (None, True),
        # stored before the episode limit was kept with the timestamps
        ([5, 999], True),
        ([5, 999, 2], False),
    ],
)
async def test_a_changed_episode_limit_brings_in_the_skipped_history(
    provider: GPodder, stored: list[int] | None, full_sync: bool
) -> None:
    """Raising the limit exposes episodes whose actions an earlier sync skipped."""
    cache: dict[str, Any] = {"sync_timestamps": stored, "feeds": [FEED]}
    mass = cast("Mock", provider.mass)
    mass.cache.get = AsyncMock(side_effect=lambda key, **_kwargs: cache.get(key))
    mass.cache.set = AsyncMock(side_effect=lambda key, data, **_kwargs: cache.update({key: data}))
    config = {"url_nc": "https://nc.example.com", "token": "token", "max_num_episodes": 2}
    cast("Mock", provider.config).get_value.side_effect = lambda key, default=None: config.get(
        key, default
    )
    history = [
        EpisodeActionPlay(
            podcast=FEED,
            episode="https://example.com/ep3.mp3",
            position=60,
            total=1200,
            timestamp="2024-01-05T10:00:00",
        )
    ]

    async def refresh(*, max_episodes: int, **_kwargs: Any) -> dict[str, Any]:
        return {**PODCAST, "episodes": PODCAST["episodes"][: max_episodes or None]}

    async def sync() -> AsyncMock:
        await provider.handle_async_init()
        _subscribe(provider, [FEED])
        get_actions = AsyncMock(side_effect=lambda since=0: (history if since == 0 else [], 999))
        cast("Mock", provider._client).get_episode_actions = get_actions
        with patch("music_assistant.providers.gpodder.refresh_cached_podcast", side_effect=refresh):
            _ = [podcast async for podcast in provider.get_library_podcasts()]
        return get_actions

    get_actions = await sync()
    assert get_actions.call_args_list == [call(since=0 if full_sync else 999)]
    played = cast("Mock", provider.mass.music).mark_item_played
    assert played.call_count == 0

    config["max_num_episodes"] = 0
    await sync()
    assert [c.args[0].item_id for c in played.call_args_list] == [f"{FEED} guid-3"]
    assert cache["sync_timestamps"] == [5, 999, 0]


async def test_sync_refreshes_several_feeds_at_once(provider: GPodder) -> None:
    """Up to the limit refresh at once, a slow feed holds up no other, a broken one is skipped."""
    _serve(provider)
    feeds = [f"https://example.com/{number}.xml" for number in range(7)]
    _subscribe(provider, feeds)
    running = peak = started = finished = 0
    others_finished = asyncio.Event()

    async def refresh(*, feed_url: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal running, peak, started, finished
        running += 1
        started += 1
        peak = max(peak, running)
        if started == 1:
            await others_finished.wait()
        else:
            await asyncio.sleep(0.01)
            finished += 1
            if finished == len(feeds) - 1:
                others_finished.set()
        running -= 1
        if feed_url == feeds[3]:
            raise MediaNotFoundError("gone")
        return PODCAST

    with patch("music_assistant.providers.gpodder.refresh_cached_podcast", side_effect=refresh):
        async with asyncio.timeout(1):
            podcasts = [podcast async for podcast in provider.get_library_podcasts()]

    assert sorted(podcast.item_id for podcast in podcasts) == sorted(set(feeds) - {feeds[3]})
    assert peak == FEED_REFRESH_CONCURRENCY


@pytest.mark.parametrize("synced", [True, False])
async def test_listing_shows_what_is_new_and_leaves_the_playlog_to_the_sync(
    provider: GPodder, synced: bool
) -> None:
    """Opening a listing repeatedly credits a finished episode only once, by the sync."""
    _serve(provider)
    _subscribe(provider, [FEED])
    if synced:
        provider.feeds = {FEED}
    finished = EpisodeActionPlay(
        podcast=FEED, episode="https://example.com/ep1.mp3", position=1200, total=1200
    )
    client = cast("Mock", provider._client)
    client.get_episode_actions = AsyncMock(return_value=([finished, *ACTIONS[:2]], 999))

    for _ in range(2):
        episodes = [x async for x in provider.get_podcast_episodes(FEED)]
        assert [_progress(x) for x in episodes] == [
            ("guid-1", True, 1_200_000),
            ("guid-2", False, 0),
            ("guid-3", None, None),
        ]
    assert [c.kwargs for c in client.get_episode_actions.call_args_list] == 2 * [
        {"since": 100 if synced else 0}
    ]
    # only the sync moves the timestamp, as only it writes every feed
    assert provider.timestamp_actions == 100
    with patch(
        "music_assistant.providers.gpodder.refresh_cached_podcast",
        AsyncMock(return_value=PODCAST),
    ):
        _ = [podcast async for podcast in provider.get_library_podcasts()]

    played = cast("Mock", provider.mass.music).mark_item_played
    assert [(c.args[0].item_id, c.kwargs["fully_played"]) for c in played.call_args_list] == [
        (f"{FEED} guid-1", True)
    ]


@pytest.mark.parametrize(
    ("action", "action_timestamp", "timestamp"),
    [
        # a mark as unplayed carries its own time, so core compares it with the playlog's
        (EpisodeActionNew, "2024-01-04T10:00:00", from_utc_timestamp(1_704_362_400)),
        # without a readable time core keeps the higher position
        (EpisodeActionNew, "", None),
        # a deleted download says nothing about progress, so a finished episode stays finished
        (EpisodeActionDelete, "2024-01-04T10:00:00", None),
    ],
)
async def test_reset_in_another_client_carries_its_own_time(
    provider: GPodder,
    action: type[EpisodeActionNew | EpisodeActionDelete],
    action_timestamp: str,
    timestamp: Any,
) -> None:
    """A reset is compared with the playlog by when it happened, not when it was fetched."""
    _serve(provider)
    reset = action(podcast=FEED, episode="https://example.com/ep2.mp3", timestamp=action_timestamp)
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
