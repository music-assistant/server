"""Tests for applying Audiobookshelf progresses against the state mass already recorded."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioaudiobookshelf.schema.media_progress import MediaProgress
from aioaudiobookshelf.schema.user import User
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, PodcastEpisode, ProviderMapping

from music_assistant.controllers.music.controller import PlaylogProviderItem
from music_assistant.providers.audiobookshelf import Audiobookshelf
from music_assistant.providers.audiobookshelf.helpers import ProgressGuard


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Iterator[_Clock]:
    """Patch the clock used by the progress guard."""
    clock = _Clock()
    with patch("music_assistant.providers.audiobookshelf.helpers.time.time", clock):
        yield clock


@pytest.fixture
def playlog_provider(provider: Audiobookshelf) -> Audiobookshelf:
    """Return a provider with a known audiobook and a mocked playlog."""
    provider.libraries.audiobooks["lib1"].item_ids.add("book1")
    provider.progress_guard = ProgressGuard()
    provider._playlog_state = {}
    provider.mass.music.mark_item_played = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.mark_item_unplayed = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock())  # type: ignore[method-assign]
    provider.mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[])  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=asyncio.ensure_future)  # type: ignore[method-assign]
    provider._pending_user_update = None
    provider._user_update_task = None
    return provider


def _progress(last_update: int, is_finished: bool, current_time: float = 3600) -> MediaProgress:
    return MediaProgress(
        id_="progress1",
        library_item_id="book1",
        duration=3600,
        current_time=current_time,
        is_finished=is_finished,
        hide_from_continue_listening=False,
        last_update=last_update,
        started_at=100,
    )


def _played_calls(provider: Audiobookshelf) -> list[bool]:
    played = cast("AsyncMock", provider.mass.music.mark_item_played)
    return [call.kwargs["fully_played"] for call in played.await_args_list]


async def test_finished_book_counted_once_across_syncs(
    playlog_provider: Audiobookshelf, clock: _Clock
) -> None:
    """The playlog sync runs four times per cycle; a finished book must count as one play."""
    progress = _progress(last_update=1000, is_finished=True)

    for _ in range(4):
        await playlog_provider._set_playlog_from_user_sync([progress])
        clock.now += 3600
        # what mass recorded in the first run is what it reports back on every later one
        cast(
            "AsyncMock", playlog_provider.mass.music.get_playlog_provider_item_ids
        ).return_value = [PlaylogProviderItem(MediaType.AUDIOBOOK, "book1", True)]

    assert _played_calls(playlog_provider) == [True]


async def test_finished_book_not_recounted_after_restart(playlog_provider: Audiobookshelf) -> None:
    """A restart empties the in-memory state; mass' own playlog still prevents a second count."""
    cast("AsyncMock", playlog_provider.mass.music.get_playlog_provider_item_ids).return_value = [
        PlaylogProviderItem(MediaType.AUDIOBOOK, "book1", True)
    ]
    playlog_provider._playlog_state = {}

    await playlog_provider._set_playlog_from_user_sync(
        [_progress(last_update=1000, is_finished=True)]
    )

    assert _played_calls(playlog_provider) == []


async def test_book_finished_again_after_being_reset_counts_again(
    playlog_provider: Audiobookshelf, clock: _Clock
) -> None:
    """A book restarted and finished a second time is a second play."""
    await playlog_provider._set_playlog_from_user_sync(
        [_progress(last_update=1000, is_finished=True)]
    )
    clock.now += 3600
    cast("AsyncMock", playlog_provider.mass.music.get_playlog_provider_item_ids).return_value = [
        PlaylogProviderItem(MediaType.AUDIOBOOK, "book1", False)
    ]

    await playlog_provider._set_playlog_from_user_sync(
        [_progress(last_update=2000, is_finished=True)]
    )

    assert _played_calls(playlog_provider) == [True, True]


async def test_position_update_of_a_finished_book_is_applied(
    playlog_provider: Audiobookshelf,
) -> None:
    """Resuming a finished book reaches mass; only a repeated finish is suppressed."""
    cast("AsyncMock", playlog_provider.mass.music.get_playlog_provider_item_ids).return_value = [
        PlaylogProviderItem(MediaType.AUDIOBOOK, "book1", True)
    ]

    await playlog_provider._set_playlog_from_user_sync(
        [_progress(last_update=1000, is_finished=False, current_time=120)]
    )

    assert _played_calls(playlog_provider) == [False]


async def test_failed_report_is_retried(playlog_provider: Audiobookshelf) -> None:
    """A play mass never recorded must not be treated as applied."""
    played = cast("AsyncMock", playlog_provider.mass.music.mark_item_played)
    played.side_effect = [RuntimeError("abs unreachable"), None]
    progress = _progress(last_update=1000, is_finished=True)

    with pytest.raises(RuntimeError):
        await playlog_provider._update_playlog_book(progress)
    await playlog_provider._update_playlog_book(progress)

    assert played.await_count == 2


def _user(user_id: str, media_progress: Any) -> User:
    user = Mock(spec=User)
    user.id_ = user_id
    user.media_progress = media_progress
    return user


async def _drain(provider: Audiobookshelf) -> None:
    if provider._user_update_task is not None:
        await provider._user_update_task


async def test_progress_of_web_ui_applied(playlog_provider: Audiobookshelf) -> None:
    """A progress changed by another abs client, only sent as user update, is applied."""
    playlog_provider.abs_user_id = "user1"
    user = _user("user1", [_progress(last_update=1000, is_finished=True)])

    with patch("music_assistant.providers.audiobookshelf.USER_UPDATED_DEBOUNCE_SECONDS", 0):
        await playlog_provider._socket_abs_user_updated(user)
        await _drain(playlog_provider)

    assert _played_calls(playlog_provider) == [True]


async def test_progress_of_other_user_ignored(playlog_provider: Audiobookshelf) -> None:
    """An admin receiving another user's update does not apply its progresses."""
    playlog_provider.abs_user_id = "user1"
    user = _user("user2", [_progress(last_update=1000, is_finished=True)])

    await playlog_provider._socket_abs_user_updated(user)

    assert playlog_provider._user_update_task is None
    assert playlog_provider._pending_user_update is None
    cast("AsyncMock", playlog_provider.mass.music.mark_item_played).assert_not_called()


async def test_user_updates_coalesce_per_burst(playlog_provider: Audiobookshelf) -> None:
    """A burst of user updates syncs once with the latest one; a later update syncs again."""
    playlog_provider.abs_user_id = "user1"
    sync = AsyncMock()

    with (
        patch.object(playlog_provider, "_set_playlog_from_user", sync),
        patch("music_assistant.providers.audiobookshelf.USER_UPDATED_DEBOUNCE_SECONDS", 0),
    ):
        for marker in range(5):
            await playlog_provider._socket_abs_user_updated(_user("user1", marker))
        await _drain(playlog_provider)
        await playlog_provider._socket_abs_user_updated(_user("user1", "late"))
        await _drain(playlog_provider)

    assert [call.args[0].media_progress for call in sync.await_args_list] == [4, "late"]


def _install_dict_backed_cache(provider: Audiobookshelf) -> list[asyncio.Future[Any]]:
    store: dict[str, Any] = {}
    tasks: list[asyncio.Future[Any]] = []

    async def _cache_get(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        if key not in store:
            return None, False, False
        return store[key], True, True

    async def _cache_set(key: str, data: Any, **_kwargs: Any) -> None:
        store[key] = data.to_dict()

    def _create_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
        task: asyncio.Future[Any] = asyncio.ensure_future(target)
        tasks.append(task)
        return task

    provider.mass.cache.get_with_freshness = AsyncMock(side_effect=_cache_get)  # type: ignore[method-assign]
    provider.mass.cache.set = AsyncMock(side_effect=_cache_set)  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=_create_task)  # type: ignore[method-assign]
    return tasks


def _episode() -> PodcastEpisode:
    return PodcastEpisode(
        item_id="pod1 ep1",
        provider="audiobookshelf--test123",
        name="Episode",
        position=1,
        duration=600,
        podcast=ItemMapping(
            item_id="pod1",
            provider="audiobookshelf--test123",
            name="Podcast",
            media_type=MediaType.PODCAST,
        ),
        provider_mappings={
            ProviderMapping(
                item_id="pod1 ep1",
                provider_domain="audiobookshelf",
                provider_instance="audiobookshelf--test123",
            )
        },
    )


def _episode_progress(last_update: int) -> MediaProgress:
    return MediaProgress(
        id_="progress1",
        library_item_id="pod1",
        episode_id="ep1",
        duration=600,
        current_time=300,
        is_finished=False,
        hide_from_continue_listening=False,
        last_update=last_update,
        started_at=100,
    )


async def test_episode_fetched_once_for_repeated_progresses(
    playlog_provider: Audiobookshelf,
) -> None:
    """Progress reports of a playing episode don't fetch its podcast each time."""
    store_tasks = _install_dict_backed_cache(playlog_provider)
    get_podcast_episode = AsyncMock(return_value=_episode())

    with patch.object(playlog_provider, "get_podcast_episode", get_podcast_episode):
        await playlog_provider._update_playlog_episode(_episode_progress(last_update=1000))
        await asyncio.gather(*store_tasks)
        await playlog_provider._update_playlog_episode(_episode_progress(last_update=16000))

    get_podcast_episode.assert_awaited_once_with("pod1 ep1", add_progress=False)
    assert cast("AsyncMock", playlog_provider.mass.music.mark_item_played).await_count == 2
