"""Unit tests for the Sonos S1 provider discovery scheduling."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import CoreState

from music_assistant.mass import MusicAssistant
from music_assistant.providers.sonos_s1.constants import DISCOVERY_INTERVAL
from music_assistant.providers.sonos_s1.provider import SonosPlayerProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable


@dataclass
class DiscoveryHarness:
    """A bare MusicAssistant together with the timers armed on its event loop."""

    mass: MusicAssistant
    scheduled: list[asyncio.TimerHandle]

    @property
    def armed_reschedules(self) -> list[asyncio.TimerHandle]:
        """
        Return the discovery reschedules still armed, however they were scheduled.

        The reschedule is the only timer these tests arm for the long haul, so the
        delay tells it apart from the short-lived handles asyncio arms internally.
        """
        now = self.mass.loop.time()
        return [
            handle
            for handle in self.scheduled
            if not handle.cancelled() and handle.when() - now > DISCOVERY_INTERVAL / 2
        ]

    def make_provider(self) -> SonosPlayerProvider:
        """Create a SonosPlayerProvider bound to this MusicAssistant."""
        provider = object.__new__(SonosPlayerProvider)
        provider.mass = self.mass
        provider.logger = MagicMock()
        provider.config = MagicMock()
        provider.config.instance_id = "sonos_s1--abc"
        provider.config.get_value.side_effect = lambda key, *_args, **_kwargs: (
            [] if "ip" in key else False
        )
        provider._discovery_task_id = "sonos_s1_discovery_test"
        provider._unloaded = False
        return provider


@pytest.fixture
async def harness() -> AsyncGenerator[DiscoveryHarness]:
    """Create a bare MusicAssistant exposing the real task/timer machinery."""
    mass = object.__new__(MusicAssistant)
    loop = asyncio.get_running_loop()
    mass.loop = loop
    mass.loop_thread_id = threading.get_ident()
    mass._tracked_timers = {}
    mass._tracked_tasks = {}
    mass._state = CoreState.RUNNING
    scheduled: list[asyncio.TimerHandle] = []
    original_call_later = loop.call_later

    # record every handle armed on the loop, so a reschedule that bypasses the
    # tracked timers is still visible to the assertions
    def _recording_call_later(
        delay: float, callback: Callable[..., object], *args: Any, **kwargs: Any
    ) -> asyncio.TimerHandle:
        handle = original_call_later(delay, callback, *args, **kwargs)
        scheduled.append(handle)
        return handle

    loop.call_later = _recording_call_later  # type: ignore[assignment,method-assign]
    harness = DiscoveryHarness(mass, scheduled)
    yield harness
    loop.call_later = original_call_later  # type: ignore[method-assign]
    # only the reschedules are ours to disarm: the rest of the recorded handles
    # belong to asyncio itself
    for handle in harness.armed_reschedules:
        handle.cancel()
    for task in mass._tracked_tasks.values():
        task.cancel()


async def test_repeated_discovery_arms_a_single_reschedule(harness: DiscoveryHarness) -> None:
    """Every load runs discovery twice, which must not leave a second reschedule armed."""
    provider = harness.make_provider()

    with patch("music_assistant.providers.sonos_s1.provider.discover", return_value=set()):
        await provider.discover_players()
        await provider.discover_players()

    assert len(harness.armed_reschedules) == 1


async def test_unload_disarms_the_reschedule(harness: DiscoveryHarness) -> None:
    """An unloaded provider must not keep a discovery reschedule armed."""
    provider = harness.make_provider()

    with patch("music_assistant.providers.sonos_s1.provider.discover", return_value=set()):
        await provider.discover_players()
        await provider.discover_players()
        with patch("music_assistant.providers.sonos_s1.provider.events_asyncio") as events:
            events.event_listener = None
            await provider.unload()

    assert harness.armed_reschedules == []


async def test_unload_aborts_a_reschedule_that_already_fired(harness: DiscoveryHarness) -> None:
    """A rescheduled discovery that already started must be cancelled by the unload."""
    provider = harness.make_provider()
    loop = asyncio.get_running_loop()
    scanning = asyncio.Event()
    unload_reached = threading.Event()

    def _blocking_discover(*_args: object, **_kwargs: object) -> set[object]:
        loop.call_soon_threadsafe(scanning.set)
        # hold the worker thread so the scan is unmistakably in flight during the unload
        assert unload_reached.wait(timeout=5)
        return set()

    with patch("music_assistant.providers.sonos_s1.provider.discover", _blocking_discover):
        harness.mass.call_later(0, provider.discover_players, task_id=provider._discovery_task_id)
        await scanning.wait()
        task = harness.mass._tracked_tasks[provider._discovery_task_id]

        with patch("music_assistant.providers.sonos_s1.provider.events_asyncio") as events:
            events.event_listener = None
            unload_task = asyncio.create_task(provider.unload())
            # the unload waits out the scan already running in its worker thread
            await asyncio.sleep(0)
            assert not unload_task.done()
            unload_reached.set()
            async with asyncio.timeout(5):
                await unload_task

    assert task.cancelled()
    assert harness.armed_reschedules == []


async def test_unload_stops_an_untracked_discovery_from_rescheduling(
    harness: DiscoveryHarness,
) -> None:
    """A discovery the provider does not own must not reschedule itself after the unload."""
    provider = harness.make_provider()
    loop = asyncio.get_running_loop()
    scanning = asyncio.Event()
    unload_reached = threading.Event()

    def _blocking_discover(*_args: object, **_kwargs: object) -> set[object]:
        loop.call_soon_threadsafe(scanning.set)
        assert unload_reached.wait(timeout=5)
        return set()

    with patch("music_assistant.providers.sonos_s1.provider.discover", _blocking_discover):
        # the discovery run after a provider load is awaited directly, so it is not
        # tracked under the provider's task id and the unload cannot cancel it
        post_load = asyncio.create_task(provider.discover_players())
        await scanning.wait()

        with patch("music_assistant.providers.sonos_s1.provider.events_asyncio") as events:
            events.event_listener = None
            unload_task = asyncio.create_task(provider.unload())
            await asyncio.sleep(0)
            unload_reached.set()
            async with asyncio.timeout(5):
                await unload_task
        async with asyncio.timeout(5):
            await post_load

    assert harness.armed_reschedules == []
