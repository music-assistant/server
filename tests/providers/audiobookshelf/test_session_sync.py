"""Tests for the Audiobookshelf session sync and cleanup."""

from __future__ import annotations

import asyncio
import time
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aioaudiobookshelf.exceptions import ServiceUnavailableError as AbsServiceUnavailableError
from aioaudiobookshelf.exceptions import SessionNotFoundError as AbsSessionNotFoundError
from aioaudiobookshelf.exceptions import SessionSyncError as AbsSessionSyncError
from aioaudiobookshelf.exceptions import (
    SessionSyncNotFoundError as AbsSessionSyncNotFoundError,
)
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import PodcastEpisode

from music_assistant.providers.audiobookshelf import Audiobookshelf
from music_assistant.providers.audiobookshelf.helpers import SessionHelper


def _make_episode() -> Mock:
    """Create a minimal podcast episode as returned by the provider."""
    episode = Mock(spec=PodcastEpisode)
    episode.duration = 600
    episode.name = "Test Episode"
    return episode


def _install_session(provider: Audiobookshelf) -> SessionHelper:
    """Add a session helper to the provider, keyed by mass item id."""
    # handle_async_init() normally initializes these, but the fixture skips it.
    # The progress guard would throttle rapid test calls, so bypass it.
    provider.sessions = {}
    provider.progress_guard = Mock()
    provider.progress_guard.guard_ok_mass.return_value = True
    # the non-session fallback path also updates media progress
    provider._client.update_my_media_progress = AsyncMock()  # type: ignore[method-assign]
    session = SessionHelper(
        abs_session_id="abs_session_1", device_slot=0, last_sync_time=time.time()
    )
    provider.sessions["pod1 ep1"] = session
    return session


async def _play(provider: Audiobookshelf) -> None:
    """Invoke on_played for a podcast episode at position 100."""
    await provider.on_played(
        media_type=MediaType.PODCAST_EPISODE,
        prov_item_id="pod1 ep1",
        fully_played=False,
        position=100,
        media_item=_make_episode(),
    )


@pytest.mark.parametrize(
    ("error", "kept"), [(AbsSessionSyncError, True), (AbsSessionSyncNotFoundError, False)]
)
async def test_session_removed_only_when_gone(
    provider: Audiobookshelf, error: type[Exception], kept: bool
) -> None:
    """A failing sync falls back to the progress update, only a session gone in abs is removed."""
    _install_session(provider)
    provider._client.sync_open_session = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    for _ in range(10):
        await _play(provider)

    assert ("pod1 ep1" in provider.sessions) is kept
    assert cast("AsyncMock", provider._client.update_my_media_progress).await_count == 10


async def test_late_sync_404_keeps_replacement_session(provider: Audiobookshelf) -> None:
    """A 404 for a session replaced while its sync was pending leaves the replacement."""
    _install_session(provider)
    replacement = SessionHelper(
        abs_session_id="abs_session_2", device_slot=0, last_sync_time=time.time()
    )

    async def _replace_then_fail(**_: object) -> None:
        provider.sessions["pod1 ep1"] = replacement
        raise AbsSessionSyncNotFoundError

    provider._client.sync_open_session = AsyncMock(side_effect=_replace_then_fail)  # type: ignore[method-assign]

    await _play(provider)

    assert provider.sessions["pod1 ep1"] is replacement


@pytest.mark.parametrize(
    ("close_result", "kept"),
    [(None, False), (AbsSessionNotFoundError, False), (AbsServiceUnavailableError, True)],
)
async def test_stale_session_removed_only_when_closed(
    provider: Audiobookshelf, close_result: type[Exception] | None, kept: bool
) -> None:
    """A stale session stays until abs confirms it closed, so its device is not reused."""
    session = _install_session(provider)
    session.last_sync_time = 0
    provider.create_session_lock = asyncio.Lock()
    provider._client.close_open_session = AsyncMock(side_effect=close_result)  # type: ignore[method-assign]

    with (
        patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider._cleanup_open_sessions_loop()

    assert ("pod1 ep1" in provider.sessions) is kept


async def test_fully_played_with_session_marks_finished(
    provider: Audiobookshelf,
) -> None:
    """Finishing an item with an open session marks it finished in abs."""
    _install_session(provider)
    sync_open_session = AsyncMock()
    provider._client.sync_open_session = sync_open_session  # type: ignore[method-assign]
    update_my_media_progress = AsyncMock()
    provider._client.update_my_media_progress = update_my_media_progress  # type: ignore[method-assign]

    await provider.on_played(
        media_type=MediaType.PODCAST_EPISODE,
        prov_item_id="pod1 ep1",
        fully_played=True,
        position=600,
        media_item=_make_episode(),
    )

    sync_open_session.assert_awaited_once()
    update_my_media_progress.assert_awaited_once_with(
        item_id="pod1",
        episode_id="ep1",
        duration_seconds=600,
        progress_seconds=600,
        is_finished=True,
    )


@pytest.mark.parametrize("has_progress", [True, False])
async def test_marked_unplayed_episode_gets_no_new_progress(
    provider: Audiobookshelf, has_progress: bool
) -> None:
    """Marking an episode unplayed removes its progress, and never creates one at 0."""
    _install_session(provider)
    # without a session, the progress update falls back to the call that would create one
    provider.sessions.clear()
    progress = Mock(id_="progress1") if has_progress else None
    provider._client.get_my_media_progress = AsyncMock(return_value=progress)  # type: ignore[method-assign]
    remove_my_media_progress = AsyncMock()
    provider._client.remove_my_media_progress = remove_my_media_progress  # type: ignore[method-assign]

    await provider.on_played(
        media_type=MediaType.PODCAST_EPISODE,
        prov_item_id="pod1 ep1",
        fully_played=False,
        position=0,
        media_item=_make_episode(),
    )

    assert remove_my_media_progress.await_count == int(has_progress)
    cast("AsyncMock", provider._client.update_my_media_progress).assert_not_awaited()
