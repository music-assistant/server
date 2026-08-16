"""Unit tests for the Sonos S1 player provider."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_provider() -> tuple[SonosPlayerProvider, MagicMock]:
    """Create a provider bound to a mocked server, and return both."""
    mass = MagicMock()
    mass.players.get_player.return_value = None
    mass.config.get_raw_player_config_value.return_value = True
    provider = object.__new__(SonosPlayerProvider)
    provider.mass = mass
    provider.logger = logging.getLogger("test.sonos_s1.provider")
    return provider, mass


def _make_soco(ip_address: str = "127.0.0.1") -> MagicMock:
    """Create a mocked soco device as discovery hands it over."""
    soco = MagicMock()
    soco.uid = "RINCON_000E58AAAAAA01400"
    soco.ip_address = ip_address
    soco.is_visible = True
    soco.fixed_volume = False
    # a freshly discovered device has not been asked for its details yet
    soco.speaker_info = {}
    return soco


async def test_disabled_speaker_is_not_interrogated() -> None:
    """A speaker the user disabled must not be queried over the network at all."""
    provider, mass = _make_provider()
    mass.config.get_raw_player_config_value.return_value = False
    soco = _make_soco()

    with patch("music_assistant.providers.sonos_s1.provider.SonosPlayer") as player_cls:
        await provider._setup_player(soco)

    soco.get_speaker_info.assert_not_called()
    player_cls.assert_not_called()


async def test_invisible_speaker_is_not_interrogated() -> None:
    """A bridge or stereo-pair follower is skipped before it is queried any further."""
    provider, _ = _make_provider()
    soco = _make_soco()
    soco.is_visible = False

    with patch("music_assistant.providers.sonos_s1.provider.SonosPlayer") as player_cls:
        await provider._setup_player(soco)

    soco.get_speaker_info.assert_not_called()
    player_cls.assert_not_called()


@pytest.mark.parametrize("fixed_volume", [False, True])
async def test_new_speaker_is_registered(fixed_volume: bool) -> None:
    """A newly discovered speaker is built with the volume mode it was interrogated for."""
    provider, _ = _make_provider()
    soco = _make_soco()
    soco.fixed_volume = fixed_volume

    with patch("music_assistant.providers.sonos_s1.provider.SonosPlayer") as player_cls:
        player_cls.return_value.setup = AsyncMock()
        await provider._setup_player(soco)

    soco.get_speaker_info.assert_called_once()
    player_cls.assert_called_once_with(provider, soco, fixed_volume)
    player_cls.return_value.setup.assert_awaited_once()


async def test_speaker_found_at_a_new_address_is_handed_the_rediscovered_device() -> None:
    """A known speaker seen at another address adopts the device discovery just built."""
    provider, mass = _make_provider()
    existing = MagicMock()
    existing.soco.ip_address = "127.0.0.1"
    existing.update_ip = AsyncMock()
    mass.players.get_player.return_value = existing
    soco = _make_soco("127.0.0.2")

    await provider._setup_player(soco)

    existing.update_ip.assert_awaited_once_with(soco)


async def test_known_speaker_at_the_same_address_is_left_alone() -> None:
    """Rediscovering a speaker at the address it already uses changes nothing."""
    provider, mass = _make_provider()
    existing = MagicMock()
    existing.soco.ip_address = "127.0.0.1"
    existing.update_ip = AsyncMock()
    mass.players.get_player.return_value = existing

    await provider._setup_player(_make_soco("127.0.0.1"))

    existing.update_ip.assert_not_awaited()
