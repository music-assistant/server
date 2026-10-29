"""Tests for applying Audiobookshelf progresses to mass."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioaudiobookshelf.exceptions import NotFoundError as AbsNotFoundError
from aioaudiobookshelf.schema.media_progress import MediaProgress
from aioaudiobookshelf.schema.user import User
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, PodcastEpisode, ProviderMapping

from music_assistant.providers.audiobookshelf import Audiobookshelf
from music_assistant.providers.audiobookshelf.helpers import ProgressGuard


@pytest.fixture
def playlog_provider(provider: Audiobookshelf) -> Audiobookshelf:
    """Return a provider with a known audiobook and a mocked playlog."""
    provider.libraries.audiobooks["lib1"].item_ids.add("book1")
    provider.progress_guard = ProgressGuard()
    provider.mass.music.mark_item_played = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.mark_item_unplayed = AsyncMock()  # type: ignore[method-assign]
    provider.mass.music.get_library_item_by_prov_id = AsyncMock(return_value=Mock())  # type: ignore[method-assign]
    provider.mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[])  # type: ignore[method-assign]
    provider.mass.create_task = Mock(side_effect=asyncio.ensure_future)  # type: ignore[method-assign]
    provider._pending_user_update = None
    provider._user_update_task = None
    return provider


def _progress(library_item_id: str | None = "book1") -> MediaProgress:
    return MediaProgress(
        id_="progress1",
        library_item_id=library_item_id,
        duration=3600,
        current_time=3600,
        is_finished=True,
        hide_from_continue_listening=False,
        last_update=1000,
        started_at=100,
    )


def _user(user_id: str, media_progress: Any) -> User:
    user = Mock(spec=User)
    user.id_ = user_id
    user.media_progress = media_progress
    return user


async def _drain(provider: Audiobookshelf) -> None:
    if provider._user_update_task is not None:
        await provider._user_update_task


@pytest.mark.parametrize(
    ("user_id", "library_item_id", "played"),
    [("user1", "book1", [True]), ("user2", "book1", []), ("user1", None, [])],
)
async def test_progress_of_web_ui_applied(
    playlog_provider: Audiobookshelf,
    user_id: str,
    library_item_id: str | None,
    played: list[bool],
) -> None:
    """
    A progress changed by another abs client is applied.

    Not if it's another user's, or abs could not map it to an item.
    """
    playlog_provider.abs_user_id = "user1"

    with patch("music_assistant.providers.audiobookshelf.USER_UPDATED_DEBOUNCE_SECONDS", 0):
        await playlog_provider._socket_abs_user_updated(
            _user(user_id, [_progress(library_item_id)])
        )
        await _drain(playlog_provider)

    mark_item_played = cast("AsyncMock", playlog_provider.mass.music.mark_item_played)
    assert [call.kwargs["fully_played"] for call in mark_item_played.await_args_list] == played


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


def _episode_progress() -> MediaProgress:
    return MediaProgress(
        id_="progress1",
        library_item_id="pod1",
        episode_id="ep1",
        duration=600,
        current_time=300,
        is_finished=False,
        hide_from_continue_listening=False,
        last_update=1000,
        started_at=100,
    )


async def test_episode_fetched_once_for_repeated_progresses(
    playlog_provider: Audiobookshelf,
) -> None:
    """Progress reports of a playing episode don't fetch its podcast each time."""
    store_tasks = _install_dict_backed_cache(playlog_provider)
    get_podcast_episode = AsyncMock(return_value=_episode())

    with patch.object(playlog_provider, "get_podcast_episode", get_podcast_episode):
        await playlog_provider._update_playlog_episode(_episode_progress())
        await asyncio.gather(*store_tasks)
        await playlog_provider._update_playlog_episode(_episode_progress())

    get_podcast_episode.assert_awaited_once_with("pod1 ep1", add_progress=False)
    assert cast("AsyncMock", playlog_provider.mass.music.mark_item_played).await_count == 2


async def test_progress_of_a_deleted_podcast_is_skipped(playlog_provider: Audiobookshelf) -> None:
    """A progress of a podcast deleted on abs doesn't end the playlog sync."""
    _install_dict_backed_cache(playlog_provider)
    playlog_provider._client.get_library_item_podcast = AsyncMock(side_effect=AbsNotFoundError)  # type: ignore[method-assign]

    await playlog_provider._update_playlog_episode(_episode_progress())

    cast("AsyncMock", playlog_provider.mass.music.mark_item_played).assert_not_called()
