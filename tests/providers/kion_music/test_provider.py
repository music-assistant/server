"""
Unit tests for KionMusicProvider (provider.py).

These tests construct a partial provider instance via ``__new__`` (no
``__init__``), attach the attributes the method-under-test reads, and
exercise it directly, so the upstream provider-init machinery does not run.
The cache decorator does need a server, so those tests attach the minimal
``MusicAssistant`` instance from the ``mass_minimal`` fixture.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from yandex_music import Track as YandexTrack

from music_assistant.models.music_provider import SYNC_RUN_STATE, SyncRunState
from music_assistant.providers.kion_music.constants import (
    LIKED_TRACKS_PLAYLIST_ID,
    MY_WAVE_PLAYLIST_ID,
)
from music_assistant.providers.kion_music.provider import KionMusicProvider

from .conftest import DE_JSON_CLIENT

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant


class _StubConfig:
    """Minimal provider config for the @use_cache decorator."""

    instance_id = "kion_music_test"

    def get_value(self, key: str, default: Any = None) -> Any:
        """Return the default for every config key."""
        return default


@pytest.fixture
async def cached_provider(
    mass_minimal: MusicAssistant,
) -> tuple[KionMusicProvider, mock.AsyncMock]:
    """Return a provider with a mocked API client, backed by a real (empty) cache."""
    await mass_minimal.cache._setup_database()
    provider = KionMusicProvider.__new__(KionMusicProvider)
    mock_client = mock.AsyncMock()
    mock_client.user_id = 12345
    provider._client = mock_client
    provider.logger = mock.MagicMock()
    provider.mass = mass_minimal
    provider.config = _StubConfig()  # type: ignore[assignment]
    provider.manifest = mock.MagicMock(domain="kion_music")
    provider._my_wave_lock = asyncio.Lock()
    provider._my_wave_seen_track_ids = set()
    provider._my_wave_radio_started_sent = False
    provider._my_wave_playlist_next_cursor = None
    return provider, mock_client


async def _wait_for_gated_fetch(started: Callable[[], bool]) -> None:
    """Wait until the gated fetch runs, then let the other callers catch up with it."""
    for _ in range(200):
        if started():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("gated fetch never started")
    # a caller arriving after the gate is released would start a second fetch,
    # which the await-count assertions below catch
    await asyncio.sleep(0.05)


async def _cache_key_exists(provider: KionMusicProvider, key: str) -> bool:
    """Return whether the provider cache contains a key after pending writes settle."""
    for _ in range(200):
        _, _, found = await provider.mass.cache.get_with_freshness(
            key,
            provider=provider.instance_id,
            include_expired=True,
        )
        if found:
            return True
        await asyncio.sleep(0.01)
    return False


async def test_playlist_kinds_use_helper_specific_cache_keys(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock],
) -> None:
    """Regular and My Mix results are cached at their helper boundaries."""
    provider, mock_client = cached_provider
    mock_client.get_playlist.return_value = type("PL", (), {"tracks": [], "track_count": 0})()
    mock_client.get_my_wave_tracks.return_value = ([], None)

    assert await provider.get_playlist_tracks("12345:67") == []
    assert await provider.get_playlist_tracks(MY_WAVE_PLAYLIST_ID) == []

    assert await _cache_key_exists(provider, "_get_regular_playlist_tracks.12345:67.0")
    assert await _cache_key_exists(provider, "_get_my_wave_playlist_tracks.0")


async def test_regular_playlist_fetch_is_shared_between_callers(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock],
) -> None:
    """Concurrent callers for the same regular playlist share one provider fetch."""
    provider, mock_client = cached_provider
    gate = asyncio.Event()

    async def _get_playlist(*_args: Any, **_kwargs: Any) -> Any:
        await gate.wait()
        return type("PL", (), {"tracks": [], "track_count": 0})()

    mock_client.get_playlist = mock.AsyncMock(side_effect=_get_playlist)

    tasks = [asyncio.create_task(provider.get_playlist_tracks("12345:67")) for _ in range(3)]
    await _wait_for_gated_fetch(lambda: mock_client.get_playlist.await_count > 0)
    gate.set()

    assert await asyncio.gather(*tasks) == [[], [], []]
    assert mock_client.get_playlist.await_count == 1


async def test_my_mix_fetch_is_shared_between_callers(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock],
) -> None:
    """Concurrent My Mix callers share one fetch, so the rotor advances once."""
    provider, mock_client = cached_provider
    gate = asyncio.Event()

    async def _get_my_wave_tracks(*_args: Any, **_kwargs: Any) -> tuple[list[Any], None]:
        await gate.wait()
        return [], None

    mock_client.get_my_wave_tracks = mock.AsyncMock(side_effect=_get_my_wave_tracks)

    tasks = [
        asyncio.create_task(provider.get_playlist_tracks(MY_WAVE_PLAYLIST_ID)) for _ in range(3)
    ]
    await _wait_for_gated_fetch(lambda: mock_client.get_my_wave_tracks.await_count > 0)
    gate.set()

    assert await asyncio.gather(*tasks) == [[], [], []]
    assert mock_client.get_my_wave_tracks.await_count == 1


@pytest.mark.parametrize(
    ("playlist_id", "backend_method"),
    [
        ("12345:67", "get_playlist"),
        (LIKED_TRACKS_PLAYLIST_ID, "get_liked_tracks"),
        (MY_WAVE_PLAYLIST_ID, "get_my_wave_tracks"),
    ],
)
async def test_playlist_page_after_first_terminates_without_backend_fetch(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock],
    playlist_id: str,
    backend_method: str,
) -> None:
    """All playlist kinds stop pagination without another backend request."""
    provider, mock_client = cached_provider

    assert await provider.get_playlist_tracks(playlist_id, page=1) == []

    getattr(mock_client, backend_method).assert_not_awaited()


async def test_failed_shared_fetch_is_not_cached(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock],
) -> None:
    """A failed shared regular-playlist request is retried on the next call."""
    provider, mock_client = cached_provider
    gate = asyncio.Event()

    async def _failing_get_playlist(*_args: Any, **_kwargs: Any) -> Any:
        await gate.wait()
        raise ResourceTemporarilyUnavailable("backend failed")

    mock_client.get_playlist.side_effect = _failing_get_playlist

    tasks = [asyncio.create_task(provider.get_playlist_tracks("12345:67")) for _ in range(3)]
    await _wait_for_gated_fetch(lambda: mock_client.get_playlist.await_count > 0)
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(isinstance(result, ResourceTemporarilyUnavailable) for result in results)
    assert mock_client.get_playlist.await_count == 1

    mock_client.get_playlist.side_effect = None
    mock_client.get_playlist.return_value = type("PL", (), {"tracks": [], "track_count": 0})()
    assert await provider.get_playlist_tracks("12345:67") == []
    assert mock_client.get_playlist.await_count == 2


async def test_cancelled_waiter_does_not_cancel_shared_fetch(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock],
) -> None:
    """Cancelling one waiter leaves the shared fetch and its cache write intact."""
    provider, mock_client = cached_provider
    gate = asyncio.Event()

    async def _get_playlist(*_args: Any, **_kwargs: Any) -> Any:
        await gate.wait()
        return type("PL", (), {"tracks": [], "track_count": 0})()

    mock_client.get_playlist.side_effect = _get_playlist
    cancelled = asyncio.create_task(provider.get_playlist_tracks("12345:67"))
    survivor = asyncio.create_task(provider.get_playlist_tracks("12345:67"))
    await _wait_for_gated_fetch(lambda: mock_client.get_playlist.await_count > 0)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    gate.set()

    assert await survivor == []
    assert await _cache_key_exists(provider, "_get_regular_playlist_tracks.12345:67.0")
    assert await provider.get_playlist_tracks("12345:67") == []
    assert mock_client.get_playlist.await_count == 1


@pytest.mark.parametrize("returned_ids", [["101"], []])
async def test_library_preserves_omitted_likes_without_warnings(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock], returned_ids: list[str]
) -> None:
    """Successful partial and empty responses retain liked IDs without failure reports."""
    provider, client = cached_provider
    client.get_liked_tracks.return_value = [
        SimpleNamespace(track_id="101:201"),
        SimpleNamespace(track_id="102:202"),
    ]
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/tracks/with_artist_and_album.json").read_text()
    )
    client.get_tracks.return_value = [
        YandexTrack.de_json({**fixture, "id": item_id}, DE_JSON_CLIENT) for item_id in returned_ids
    ]
    state = SyncRunState()
    token = SYNC_RUN_STATE.set(state)
    try:
        result = [track async for track in provider.get_library_tracks()]
        assert [track.item_id for track in result] == returned_ids
        assert state.skipped_item_ids[MediaType.TRACK] == {"101", "102"} - set(returned_ids)
        assert state.failures == 0
        cast("mock.MagicMock", provider.logger).warning.assert_not_called()
    finally:
        SYNC_RUN_STATE.reset(token)


async def test_library_batch_failure_aborts_without_omission_reports(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock],
) -> None:
    """A request failure aborts listing without fabricating per-track sync failures."""
    provider, client = cached_provider
    client.get_liked_tracks.return_value = [SimpleNamespace(track_id="101")]
    client.get_tracks.side_effect = ResourceTemporarilyUnavailable("batch rejected")
    state = SyncRunState()
    token = SYNC_RUN_STATE.set(state)
    try:
        with pytest.raises(ResourceTemporarilyUnavailable):
            _ = [track async for track in provider.get_library_tracks()]
        assert state.skipped_item_ids == {}
        assert state.failures == 0
        cast("mock.MagicMock", provider.logger).warning.assert_not_called()
    finally:
        SYNC_RUN_STATE.reset(token)


async def test_liked_playlist_parse_failure_does_not_change_library_sync_state(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock],
) -> None:
    """Browsing an invalid liked track must not report a library sync failure."""
    provider, client = cached_provider
    fixture = json.loads((Path(__file__).parent / "fixtures/tracks/minimal.json").read_text())
    fixture["artists"] = [{"id": None, "name": "Unidentifiable artist"}]
    client.get_liked_tracks.return_value = [SimpleNamespace(track_id="400")]
    client.get_tracks.return_value = [YandexTrack.de_json(fixture, DE_JSON_CLIENT)]
    state = SyncRunState()
    token = SYNC_RUN_STATE.set(state)
    try:
        assert await provider.get_playlist_tracks(LIKED_TRACKS_PLAYLIST_ID) == []
        assert state.failures == 0
        assert state.skipped_item_ids == {}
        cast("mock.MagicMock", provider.logger).warning.assert_not_called()
    finally:
        SYNC_RUN_STATE.reset(token)
