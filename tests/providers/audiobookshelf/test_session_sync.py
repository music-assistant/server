"""Tests for the Audiobookshelf session sync retry logic."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, Mock

from aioaudiobookshelf.exceptions import SessionSyncError as AbsSessionSyncError
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
    session = SessionHelper(abs_session_id="abs_session_1", last_sync_time=time.time())
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


async def test_session_kept_below_failure_threshold(
    provider: Audiobookshelf,
) -> None:
    """A session survives consecutive failures below the threshold."""
    _install_session(provider)
    provider._client.sync_open_session = AsyncMock(  # type: ignore[method-assign]
        side_effect=AbsSessionSyncError()
    )

    for _ in range(4):
        await _play(provider)

    assert "pod1 ep1" in provider.sessions
    session = provider.sessions["pod1 ep1"]
    assert session.failed_sync_count == 4


async def test_session_removed_at_failure_threshold(
    provider: Audiobookshelf,
) -> None:
    """A session is removed after the 5th consecutive failure."""
    _install_session(provider)
    provider._client.sync_open_session = AsyncMock(  # type: ignore[method-assign]
        side_effect=AbsSessionSyncError()
    )

    for _ in range(5):
        await _play(provider)

    assert "pod1 ep1" not in provider.sessions


async def test_failed_sync_count_reset_on_success(
    provider: Audiobookshelf,
) -> None:
    """A successful sync resets the consecutive failure counter."""
    session = _install_session(provider)
    # fail twice, then succeed
    provider._client.sync_open_session = AsyncMock(  # type: ignore[method-assign]
        side_effect=[AbsSessionSyncError(), AbsSessionSyncError(), Mock()]
    )

    for _ in range(2):
        await _play(provider)
    assert session.failed_sync_count == 2

    await _play(provider)
    assert session.failed_sync_count == 0
    assert "pod1 ep1" in provider.sessions
