"""
Macro benchmarks for server lifecycle and library sync.

Each benchmark measures one expensive core operation end-to-end on a hermetic
MusicAssistant instance: server boot, the initial sync of a music provider and
a no-op re-sync of an already synced library. The noop resync is the key sync
regression metric: it must stay cheap because it runs on every scheduled sync.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .conftest import (
    SMALL_LIBRARY_SIZE,
    expected_track_count,
    sync_test_provider,
    wait_for_sync_complete,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.mark.benchmark
@pytest.mark.single_run
async def test_server_boot(unstarted_mass: MusicAssistant) -> None:
    """Benchmark a cold server boot: all core controllers + builtin providers."""
    await unstarted_mass.start()


@pytest.mark.benchmark
@pytest.mark.single_run
async def test_initial_sync(started_mass: MusicAssistant) -> None:
    """Benchmark the initial library sync of a freshly added music provider."""
    await sync_test_provider(started_mass, SMALL_LIBRARY_SIZE)


@pytest.mark.benchmark
async def test_noop_resync(synced_mass: MusicAssistant) -> None:
    """Benchmark re-syncing an already synced library (should be near a no-op)."""
    await synced_mass.music.start_sync()
    await wait_for_sync_complete(synced_mass, expected_track_count(SMALL_LIBRARY_SIZE))
