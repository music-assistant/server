"""
Macro benchmarks for server lifecycle and library sync.

Each benchmark measures one expensive core operation end-to-end on a hermetic
MusicAssistant instance. The bodies are self-contained (each run boots its own
fresh instance where needed) so they can safely be invoked multiple times by
the measurement tool (warmup + measured rounds).
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
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from music_assistant.mass import MusicAssistant

    ServerFactory = Callable[[], AbstractAsyncContextManager[MusicAssistant]]


@pytest.mark.benchmark
async def test_server_boot(hermetic_server: ServerFactory) -> None:
    """Benchmark a full server lifecycle: boot all core controllers, then shutdown."""
    async with hermetic_server() as mass:
        await mass.start()


@pytest.mark.benchmark
async def test_initial_sync(hermetic_server: ServerFactory) -> None:
    """
    Benchmark boot plus the initial library sync of a freshly added music provider.

    Compare against ``test_server_boot`` to isolate the sync cost itself.
    """
    async with hermetic_server() as mass:
        await mass.start()
        await sync_test_provider(mass, SMALL_LIBRARY_SIZE)


@pytest.mark.benchmark
async def test_noop_resync(synced_mass: MusicAssistant) -> None:
    """Benchmark re-syncing an already synced library (should be near a no-op)."""
    await synced_mass.music.start_sync()
    await wait_for_sync_complete(synced_mass, expected_track_count(SMALL_LIBRARY_SIZE))
