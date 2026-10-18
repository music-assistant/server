"""Tests for skipping unchanged Apple Music library syncs."""

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, PropertyMock, call, patch

import pytest
from music_assistant_models.enums import EventType, MediaType
from music_assistant_models.errors import RetriesExhausted

from music_assistant.controllers.music.controller import MusicController
from music_assistant.models.music_provider import MusicProvider, NothingToSync
from music_assistant.providers.apple_music.constants import LIBRARY_FULL_SYNC_INTERVAL
from music_assistant.providers.apple_music.library import AppleMusicLibraryManager
from music_assistant.providers.apple_music.provider import AppleMusicProvider

MARKER = "10|abc"


def _provider(
    marker: str | Exception, state: dict[str, object] | None
) -> tuple[AppleMusicProvider, AsyncMock, AsyncMock]:
    """Return a bare provider plus its cache.set and get_sync_marker mocks."""
    cache_set = AsyncMock()
    if isinstance(marker, Exception):
        get_marker = AsyncMock(side_effect=marker)
    else:
        get_marker = AsyncMock(return_value=marker)
    provider = object.__new__(AppleMusicProvider)
    provider.mass = MagicMock(cache=Mock(get=AsyncMock(return_value=state), set=cache_set))
    provider.logger = MagicMock()
    provider.library_manager = Mock(
        get_sync_marker=get_marker,
        get_favourites_marker=AsyncMock(return_value=""),
        refresh_favourites=AsyncMock(),
    )
    return provider, cache_set, get_marker


@contextmanager
def _patched(user_id: str | None = None) -> Iterator[AsyncMock]:
    super_sync = AsyncMock()
    with (
        patch.object(MusicProvider, "sync_library", super_sync),
        patch.object(AppleMusicProvider, "instance_id", "apple_music--test"),
        patch(
            "music_assistant.providers.apple_music.provider.get_current_task",
            return_value=Mock(last_run_user_id=user_id),
        ),
    ):
        yield super_sync


async def test_unchanged_library_skips_sync() -> None:
    """An equal marker and a recent full sync raise NothingToSync."""
    provider, _, _ = _provider(MARKER, {"marker": MARKER, "full_sync_at": time.time()})
    with _patched() as super_sync, pytest.raises(NothingToSync):
        await provider.sync_library(MediaType.TRACK)
    super_sync.assert_not_awaited()


async def test_changed_library_syncs_and_stores_marker() -> None:
    """A changed marker runs the sync and stores the marker fetched before it."""
    provider, cache_set, _ = _provider(MARKER, {"marker": "old", "full_sync_at": time.time()})
    with _patched() as super_sync:
        await provider.sync_library(MediaType.TRACK)
    super_sync.assert_awaited_once_with(MediaType.TRACK)
    cache_set.assert_awaited_once()
    kwargs = cache_set.call_args.kwargs
    assert kwargs["key"] == "library_sync_state.track"
    assert kwargs["data"]["marker"] == MARKER
    assert kwargs["expiration"] == LIBRARY_FULL_SYNC_INTERVAL


async def test_old_full_sync_forces_sync() -> None:
    """An equal marker does not skip once the last full sync is older than the interval."""
    stale = time.time() - LIBRARY_FULL_SYNC_INTERVAL - 1
    provider, _, _ = _provider(MARKER, {"marker": MARKER, "full_sync_at": stale})
    with _patched() as super_sync:
        await provider.sync_library(MediaType.TRACK)
    super_sync.assert_awaited_once_with(MediaType.TRACK)


async def test_manual_run_never_skips() -> None:
    """A sync started by a user always runs."""
    provider, _, _ = _provider(MARKER, {"marker": MARKER, "full_sync_at": time.time()})
    with _patched(user_id="user") as super_sync:
        await provider.sync_library(MediaType.TRACK)
    super_sync.assert_awaited_once_with(MediaType.TRACK)


async def test_playlists_never_skip() -> None:
    """A playlist sync always runs, since a marker would miss playlist content changes."""
    provider, _, get_marker = _provider(MARKER, {"marker": MARKER, "full_sync_at": time.time()})
    with _patched() as super_sync:
        await provider.sync_library(MediaType.PLAYLIST)
    super_sync.assert_awaited_once_with(MediaType.PLAYLIST)
    get_marker.assert_not_awaited()


async def test_failed_marker_fetch_runs_full_sync_without_storing() -> None:
    """A failing marker check falls back to a full sync and stores nothing."""
    provider, cache_set, _ = _provider(RetriesExhausted("throttled"), None)
    with _patched() as super_sync:
        await provider.sync_library(MediaType.TRACK)
    super_sync.assert_awaited_once_with(MediaType.TRACK)
    cache_set.assert_not_awaited()


_MARKER_RESPONSES: dict[str, dict[str, Any]] = {
    "me/library/songs": {"meta": {"total": 845}, "data": [{"id": "i.new"}]},
    "me/library/artists": {"meta": {"total": 49}, "data": [{"id": "r.1"}]},
    "me/library/albums": {"meta": {"total": 106}, "data": [{"id": "l.1"}]},
    "me/library/recently-added": {
        "data": [{"id": "l.new", "attributes": {"dateAdded": "2026-10-12T16:53:07Z"}}]
    },
}


@pytest.mark.parametrize(
    ("media_type", "expected", "expected_calls"),
    [
        (
            MediaType.TRACK,
            "845|i.new",
            [call("me/library/songs", limit=1, sort="-dateAdded")],
        ),
        (MediaType.ARTIST, "49", [call("me/library/artists", limit=1)]),
        (
            MediaType.ALBUM,
            "106|l.new|2026-10-12T16:53:07Z|False",
            [
                call("me/library/albums", limit=1),
                call("me/library/recently-added", limit=1),
            ],
        ),
    ],
)
async def test_sync_marker(media_type: MediaType, expected: str, expected_calls: list[Any]) -> None:
    """The marker combines the library total with the newest addition where Apple has one."""
    provider = MagicMock()
    provider.library_sync_album_tracks_enabled = Mock(return_value=False)
    provider.api_client.get_data = AsyncMock(
        side_effect=lambda endpoint, **_kwargs: _MARKER_RESPONSES[endpoint]
    )
    manager = AppleMusicLibraryManager(provider)

    assert await manager.get_sync_marker(media_type) == expected
    assert provider.api_client.get_data.await_args_list == expected_calls


async def test_sync_marker_without_total_raises() -> None:
    """A listing without a total fails the check instead of producing a marker."""
    provider = MagicMock()
    provider.api_client.get_data = AsyncMock(return_value={})
    manager = AppleMusicLibraryManager(provider)

    with pytest.raises(KeyError):
        await manager.get_sync_marker(MediaType.ARTIST)


def _controller(events: list[EventType]) -> MusicController:
    ctrl = MusicController.__new__(MusicController)
    ctrl.mass = Mock()
    ctrl.mass.signal_event = Mock(side_effect=lambda event, *_a, **_k: events.append(event))
    # run the deferred completion check inline instead of on the event loop
    ctrl.mass.call_later = Mock(side_effect=lambda _delay, target, **_kwargs: target())
    ctrl.logger = Mock()
    ctrl._sync_lock = asyncio.Lock()
    ctrl._sync_completion_pending = False
    ctrl._track_reconciliation_cursor = (0, 0)
    ctrl._track_reconciliation_rescan_due = False
    return ctrl


async def test_skipped_sync_does_not_announce_completion() -> None:
    """A skipped sync alone does not emit MUSIC_SYNC_COMPLETED."""
    events: list[EventType] = []
    ctrl = _controller(events)
    provider = Mock(sync_library=AsyncMock(side_effect=NothingToSync))

    await ctrl._create_provider_sync_handler(provider, MediaType.TRACK)()

    assert events == []
    assert ctrl._sync_completion_pending is False


async def test_completion_announced_once_after_skip_follows_real_sync() -> None:
    """A real sync whose check bailed still gets announced by the skipped sync finishing last."""
    events: list[EventType] = []
    ctrl = _controller(events)
    real = Mock(sync_library=AsyncMock())
    skipped = Mock(sync_library=AsyncMock(side_effect=NothingToSync))

    with (
        patch.object(MusicController, "active_sync_tasks", new_callable=PropertyMock) as tasks,
        patch.object(ctrl, "_set_track_reconciliation_state"),
        patch.object(ctrl, "_queue_database_cleanup_task"),
        patch.object(ctrl, "cleanup_library_shortcuts", Mock()),
    ):
        tasks.return_value = ["other"]
        await ctrl._create_provider_sync_handler(real, MediaType.TRACK)()
        assert events == []
        tasks.return_value = []
        await ctrl._create_provider_sync_handler(skipped, MediaType.ALBUM)()

    assert events == [EventType.MUSIC_SYNC_COMPLETED]
    assert ctrl._sync_completion_pending is False
