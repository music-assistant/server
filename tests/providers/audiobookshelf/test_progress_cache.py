"""Tests for sharing the Audiobookshelf user progress between lookups."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from aioaudiobookshelf.schema.media_progress import MediaProgress
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import PodcastEpisode

from music_assistant.helpers.json import json_dumps, json_loads
from music_assistant.providers.audiobookshelf import Audiobookshelf

PROGRESS = MediaProgress(
    id_="progress1",
    library_item_id="pod1",
    episode_id="ep1",
    duration=600,
    current_time=30,
    is_finished=False,
    hide_from_continue_listening=False,
    last_update=1000,
    started_at=100,
)


async def test_progress_is_fetched_once_per_pass(provider: Audiobookshelf) -> None:
    """Listing podcasts back to back shares the progress, and any progress change asks again."""
    stored: dict[str, Any] = {}

    async def get_with_freshness(key: str, **_kwargs: Any) -> tuple[Any, bool, bool]:
        return (stored[key], True, True) if key in stored else (None, False, False)

    async def store(key: str, data: Any, **_kwargs: Any) -> None:
        # stored as json, as the real cache does
        stored[key] = json_loads(json_dumps(data))

    async def drop(key: str, **_kwargs: Any) -> None:
        stored.pop(key, None)

    async def drop_prefix(prefix: str, **_kwargs: Any) -> None:
        for key in [key for key in stored if key.startswith(prefix)]:
            stored.pop(key)

    get_my_user = AsyncMock(return_value=Mock(media_progress=[PROGRESS]))
    provider._client.get_my_user = get_my_user  # type: ignore[method-assign]
    provider._client.update_my_media_progress = AsyncMock()  # type: ignore[method-assign]
    provider._get_abs_expanded_podcast = AsyncMock(  # type: ignore[method-assign]
        return_value=Mock(media=Mock(episodes=[]))
    )
    provider._update_playlog_episode = AsyncMock()  # type: ignore[method-assign]
    provider._get_all_known_item_ids = Mock(return_value={"pod1"})  # type: ignore[method-assign]
    provider.sessions = {}
    provider.progress_guard = Mock()
    provider.progress_guard.guard_ok_mass.return_value = True
    provider.progress_guard.guard_ok_abs.return_value = True
    episode = Mock(spec=PodcastEpisode, duration=600)
    episode.name = "Episode"
    cache = provider.mass.cache
    with (
        patch.object(cache, "get_with_freshness", get_with_freshness),
        patch.object(cache, "set", store),
        patch.object(cache, "delete", drop),
        patch.object(cache, "delete_prefix", drop_prefix),
    ):
        _ = [x async for x in provider.get_podcast_episodes("pod1")]
        _ = [x async for x in provider.get_podcast_episodes("pod2")]
        assert await provider._media_progress() == [PROGRESS]
        assert get_my_user.await_count == 1

        await provider.on_played(MediaType.PODCAST_EPISODE, "pod1 ep1", False, 100, episode)
        _ = [x async for x in provider.get_podcast_episodes("pod1")]
        assert get_my_user.await_count == 2

        # progress made in another client arrives over the socket
        await provider._socket_abs_user_item_progress_updated("progress1", PROGRESS)
        _ = [x async for x in provider.get_podcast_episodes("pod1")]
        assert get_my_user.await_count == 3
