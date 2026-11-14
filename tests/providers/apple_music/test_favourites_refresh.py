"""Tests for refreshing Apple Music favourites on skipped library syncs."""

import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import RetriesExhausted

from music_assistant.models.music_provider import MusicProvider, NothingToSync
from music_assistant.providers.apple_music.constants import LIBRARY_FULL_SYNC_INTERVAL
from music_assistant.providers.apple_music.library import AppleMusicLibraryManager
from music_assistant.providers.apple_music.provider import AppleMusicProvider

MARKER = "10|abc"


def _provider(
    state: dict[str, object], favourites: str
) -> tuple[AppleMusicProvider, AsyncMock, AsyncMock]:
    """Return a bare provider plus its cache.set and refresh_favourites mocks."""
    cache_set = AsyncMock()
    refresh = AsyncMock()
    provider = object.__new__(AppleMusicProvider)
    provider.mass = MagicMock(cache=Mock(get=AsyncMock(return_value=state), set=cache_set))
    provider.logger = MagicMock()
    provider.library_manager = Mock(
        get_sync_marker=AsyncMock(return_value=MARKER),
        get_favourites_marker=AsyncMock(return_value=favourites),
        refresh_favourites=refresh,
    )
    return provider, cache_set, refresh


@contextmanager
def _patched() -> Iterator[AsyncMock]:
    super_sync = AsyncMock()
    with (
        patch.object(MusicProvider, "sync_library", super_sync),
        patch.object(AppleMusicProvider, "instance_id", "apple_music--test"),
        patch(
            "music_assistant.providers.apple_music.provider.get_current_task",
            return_value=Mock(last_run_user_id=None),
        ),
    ):
        yield super_sync


async def test_favourites_marker_takes_automatic_playlists() -> None:
    """Only non-editable playlists without a catalog count, sorted with their modified date."""
    provider = MagicMock()
    provider.api_client.get_all_items = AsyncMock(
        return_value=[
            _playlist("p.fav", False, False, "2026-10-14T10:00:00Z"),
            _playlist("p.own", True, False, "2026-10-13T10:00:00Z"),
            _playlist("p.apple", False, True, "2026-10-12T10:00:00Z"),
            _playlist("p.bought", False, False, "1970-01-01T00:00:00Z"),
        ]
    )
    manager = AppleMusicLibraryManager(provider)

    assert await manager.get_favourites_marker() == (
        "p.bought=1970-01-01T00:00:00Z,p.fav=2026-10-14T10:00:00Z"
    )


async def test_moved_favourites_refresh_tracks_on_skip() -> None:
    """A moved favourites marker refreshes track ratings and stores it, keeping full_sync_at."""
    full_sync_at = time.time()
    state = {"marker": MARKER, "full_sync_at": full_sync_at, "favourites": "old"}
    provider, cache_set, refresh = _provider(state, "new")
    with _patched() as super_sync, pytest.raises(NothingToSync):
        await provider.sync_library(MediaType.TRACK)
    super_sync.assert_not_awaited()
    refresh.assert_awaited_once_with(MediaType.TRACK)
    cache_set.assert_awaited_once()
    kwargs = cache_set.call_args.kwargs
    assert kwargs["data"] == {"marker": MARKER, "full_sync_at": full_sync_at, "favourites": "new"}
    assert kwargs["expiration"] == LIBRARY_FULL_SYNC_INTERVAL


async def test_unchanged_favourites_skip_without_refresh() -> None:
    """An unchanged favourites marker refreshes nothing and writes no cache."""
    state = {"marker": MARKER, "full_sync_at": time.time(), "favourites": "same"}
    provider, cache_set, refresh = _provider(state, "same")
    with _patched(), pytest.raises(NothingToSync):
        await provider.sync_library(MediaType.TRACK)
    refresh.assert_not_awaited()
    cache_set.assert_not_awaited()


async def test_skipped_album_check_refreshes_ratings() -> None:
    """A skipped album check refreshes the album ratings."""
    provider, _, refresh = _provider({"marker": MARKER, "full_sync_at": time.time()}, "")
    with _patched(), pytest.raises(NothingToSync):
        await provider.sync_library(MediaType.ALBUM)
    refresh.assert_awaited_once_with(MediaType.ALBUM)


async def test_failed_refresh_still_skips() -> None:
    """A failed refresh keeps the skip and stores nothing, so the next check retries."""
    state = {"marker": MARKER, "full_sync_at": time.time(), "favourites": "old"}
    provider, cache_set, refresh = _provider(state, "new")
    refresh.side_effect = RetriesExhausted("throttled")
    with _patched(), pytest.raises(NothingToSync):
        await provider.sync_library(MediaType.TRACK)
    cache_set.assert_not_awaited()


async def test_refresh_favourites_records_ratings() -> None:
    """Catalog and library ids are rated in separate calls and every returned rating is recorded."""
    items = [
        _track(1, "1001"),
        _track(2, "1002"),
        _track(3, "i.lib1"),
        _track(4, "1004", in_library=False),
        _track(5, "1005", instance="apple_music--other"),
    ]

    async def iter_items(*_args: Any) -> AsyncGenerator[Mock]:
        for item in items:
            yield item

    provider = MagicMock(instance_id="apple_music--test")
    provider.mass.music.tracks.iter_library_items_by_prov_id = iter_items
    provider.mass.music.favorites.record_from_provider = AsyncMock()
    ratings = {"1001": True, "i.lib1": False}
    provider.api_client.get_ratings = AsyncMock(
        side_effect=lambda ids, _media_type: {i: ratings[i] for i in ids if i in ratings}
    )
    manager = AppleMusicLibraryManager(provider)

    await manager.refresh_favourites(MediaType.TRACK)

    assert [c.args[0] for c in provider.api_client.get_ratings.await_args_list] == [
        ["1001", "1002"],
        ["i.lib1"],
    ]
    record = provider.mass.music.favorites.record_from_provider
    assert sorted(c.args for c in record.await_args_list) == [
        ("apple_music--test", MediaType.TRACK, 1, True),
        ("apple_music--test", MediaType.TRACK, 3, False),
    ]


def _playlist(item_id: str, can_edit: bool, has_catalog: bool, modified: str) -> dict[str, Any]:
    """Return a library playlist listing row."""
    attributes = {"canEdit": can_edit, "hasCatalog": has_catalog, "lastModifiedDate": modified}
    return {"id": item_id, "attributes": attributes}


def _track(
    item_id: int, prov_item_id: str, in_library: bool = True, instance: str = "apple_music--test"
) -> Mock:
    """Return a library track with one provider mapping."""
    mapping = Mock(item_id=prov_item_id, provider_instance=instance, in_library=in_library)
    return Mock(item_id=str(item_id), provider_mappings=[mapping])
