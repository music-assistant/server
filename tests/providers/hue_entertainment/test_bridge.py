"""Tests for how the Hue Lights Sync bridge starts and stops its light target."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from hue_entertainment import LightChannel

from music_assistant.providers.hue_entertainment import bridge as bridge_module
from music_assistant.providers.hue_entertainment.bridge import HueEntertainmentBridge
from music_assistant.providers.hue_entertainment.targets import TargetUnavailableError


class FakeTarget:
    """A light target whose start completes when the test allows it."""

    client_id = "hue-zb-test"
    name = "Living room"
    product_name = "Hue Lights (ZHA)"

    def __init__(self) -> None:
        """Initialize the target; its start completes right away until the test holds it."""
        self.channels = [LightChannel(channel_id=0, service_id="aa", name="aa")]
        self.start_entered = asyncio.Event()
        self.start_allowed = asyncio.Event()
        self.start_allowed.set()
        self.start_error: Exception | None = None
        self.start_calls = 0
        self.close_calls = 0
        self.streaming = False

    @property
    def is_streaming(self) -> bool:
        """Return True while streaming."""
        return self.streaming

    async def start(self) -> None:
        """Open the stream once allowed, or raise the prepared error."""
        self.start_calls += 1
        self.start_entered.set()
        await self.start_allowed.wait()
        if self.start_error is not None:
            raise self.start_error
        self.streaming = True

    def send(self, commands: list[Any]) -> None:
        """Ignore the frame."""

    async def aclose(self) -> None:
        """Stop streaming."""
        self.close_calls += 1
        self.streaming = False


@pytest.fixture
def target() -> FakeTarget:
    """Return the fake target."""
    return FakeTarget()


@pytest.fixture
async def bridge(target: FakeTarget) -> AsyncIterator[HueEntertainmentBridge]:
    """Return a bridge for the fake target, without registering it with Sendspin."""
    loop = asyncio.get_running_loop()

    def _create_task(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        return loop.create_task(coro)

    provider: Any = SimpleNamespace(
        mass=SimpleNamespace(loop=loop, create_task=_create_task),
        config=Mock(get_value=Mock(return_value=None)),
    )
    sendspin_server: Any = SimpleNamespace(clock=SimpleNamespace(now_us=lambda: 0))
    bridge = HueEntertainmentBridge(provider, target, sendspin_server)
    yield bridge
    await bridge.stop()


async def test_late_start_after_the_stream_ended_is_stopped(
    bridge: HueEntertainmentBridge, target: FakeTarget
) -> None:
    """A start that completes after the stream ended does not leave the lights streaming."""
    target.start_allowed.clear()
    bridge._on_stream_start()
    await target.start_entered.wait()

    bridge._on_stream_end()
    target.start_allowed.set()
    assert bridge._start_task is not None
    await bridge._start_task
    started = bridge._is_streaming
    assert bridge._stop_debounce_task is not None
    await bridge._stop_debounce_task

    assert started
    assert not target.streaming
    assert not bridge._is_streaming
    assert bridge._render_handle is None


async def test_start_still_running_when_the_stream_ended_is_cancelled(
    bridge: HueEntertainmentBridge, target: FakeTarget
) -> None:
    """A start that has not completed when the stop debounce ends is cancelled and closed."""
    target.start_allowed.clear()
    bridge._on_stream_start()
    await target.start_entered.wait()

    bridge._on_stream_end()
    assert bridge._stop_debounce_task is not None
    await bridge._stop_debounce_task

    assert bridge._start_task is not None
    assert bridge._start_task.cancelled()
    assert target.close_calls == 1
    assert not bridge._is_streaming


async def test_stop_during_a_start_closes_the_target(
    bridge: HueEntertainmentBridge, target: FakeTarget
) -> None:
    """Stopping the bridge while its target starts cancels the start and closes the target."""
    target.start_allowed.clear()
    bridge._on_stream_start()
    await target.start_entered.wait()

    await bridge.stop()
    target.start_allowed.set()
    await asyncio.sleep(0)

    assert target.close_calls >= 1
    assert not target.streaming
    assert not bridge._is_streaming
    assert bridge._render_handle is None


async def test_unavailable_target_is_not_retried(
    bridge: HueEntertainmentBridge, target: FakeTarget
) -> None:
    """A target that can not stream now is tried once and closed."""
    target.start_error = TargetUnavailableError("the Home Assistant plugin is not connected")

    bridge._on_stream_start()
    assert bridge._start_task is not None
    await bridge._start_task

    assert target.start_calls == 1
    assert target.close_calls == 1
    assert not bridge._is_streaming


async def test_failing_target_start_is_retried(
    monkeypatch: pytest.MonkeyPatch, bridge: HueEntertainmentBridge, target: FakeTarget
) -> None:
    """Any other start failure is retried before the bridge gives up and closes the target."""
    monkeypatch.setattr(bridge_module, "_ENTERTAINMENT_START_BACKOFF_S", 0)
    target.start_error = RuntimeError("handshake timed out")

    bridge._on_stream_start()
    assert bridge._start_task is not None
    await bridge._start_task

    assert target.start_calls == bridge_module._ENTERTAINMENT_START_ATTEMPTS
    assert target.close_calls == 1
    assert not bridge._is_streaming


async def test_started_target_streams_until_the_stream_ends(
    bridge: HueEntertainmentBridge, target: FakeTarget
) -> None:
    """A successful start streams; the debounced stream end closes the target."""
    bridge._on_stream_start()
    assert bridge._start_task is not None
    await bridge._start_task
    started = bridge._is_streaming
    rendering = bridge._render_handle is not None

    bridge._on_stream_end()
    assert bridge._stop_debounce_task is not None
    await bridge._stop_debounce_task

    assert started
    assert rendering
    assert target.close_calls == 1
    assert not target.streaming
    assert bridge._render_handle is None


async def test_frames_are_only_queued_while_the_target_streams(
    bridge: HueEntertainmentBridge, target: FakeTarget
) -> None:
    """A parked or lost stream queues no visualizer frames that no render would drain."""
    analyzer = Mock()
    bridge._analyzer = analyzer
    frame: Any = SimpleNamespace(
        spectrum=Mock(tolist=Mock(return_value=[1, 2])), peak=0.5, timestamp_us=10
    )
    bridge._on_stream_start()
    assert bridge._start_task is not None
    await bridge._start_task

    bridge._on_visualizer_frame(frame)
    target.streaming = False  # taken over by another Zigbee target, or lost
    bridge._on_visualizer_frame(frame)

    assert bridge._is_streaming
    analyzer.apply_spectrum.assert_called_once_with([1, 2], 10)
    analyzer.apply_peak.assert_called_once_with(0.5, 10)
