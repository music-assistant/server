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
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from music_assistant.providers.kion_music.constants import MY_WAVE_PLAYLIST_ID
from music_assistant.providers.kion_music.provider import KionMusicProvider

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


async def test_my_mix_fetch_runs_for_every_caller(
    cached_provider: tuple[KionMusicProvider, mock.AsyncMock],
) -> None:
    """Every My Mix caller runs its own fetch, so the rotor cursor keeps advancing."""
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
    assert mock_client.get_my_wave_tracks.await_count == 3
