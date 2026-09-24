"""Tests for applying Audiobookshelf podcast episode progresses to the playlog."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from aioaudiobookshelf.schema.media_progress import MediaProgress
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping, PodcastEpisode, ProviderMapping

from music_assistant.providers.audiobookshelf import Audiobookshelf
from music_assistant.providers.audiobookshelf.helpers import ProgressGuard


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


def _progress(last_update: int) -> MediaProgress:
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


async def test_episode_fetched_once_for_repeated_progresses(provider: Audiobookshelf) -> None:
    """Progress reports of a playing episode don't fetch its podcast each time."""
    store_tasks = _install_dict_backed_cache(provider)
    provider.progress_guard = ProgressGuard()
    provider.mass.music.mark_item_played = AsyncMock()  # type: ignore[method-assign]
    get_podcast_episode = AsyncMock(return_value=_episode())

    with patch.object(provider, "get_podcast_episode", get_podcast_episode):
        await provider._update_playlog_episode(_progress(last_update=1000))
        await asyncio.gather(*store_tasks)
        await provider._update_playlog_episode(_progress(last_update=16000))

    get_podcast_episode.assert_awaited_once_with("pod1 ep1", add_progress=False)
    assert provider.mass.music.mark_item_played.await_count == 2
