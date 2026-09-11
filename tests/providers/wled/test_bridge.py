"""Tests for the WLED sync-zone bridge's stream lifecycle and send loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from aiosendspin.server.roles.visualizer.features import ExtractedFrame

from music_assistant.providers.wled.bridge import WledBridge
from music_assistant.providers.wled.constants import SPECTRUM_BINS


class _FakeClock:
    """Minimal stand-in for SendspinServer.clock."""

    def __init__(self, now_us: int = 0) -> None:
        self._now_us = now_us

    def now_us(self) -> int:
        return self._now_us


class _FakeSendspinServer:
    """Minimal stand-in for the parts of SendspinServer the bridge touches directly."""

    def __init__(self) -> None:
        self.clock = _FakeClock()
        self.remove_client = AsyncMock()


@dataclass
class _BridgeFixture:
    """A WledBridge plus untyped handles to its fakes, so assertions skip static typing."""

    bridge: WledBridge
    call_later: Mock = field(default_factory=lambda: Mock(return_value=Mock()))
    sendspin_server: _FakeSendspinServer = field(default_factory=_FakeSendspinServer)


def _make_bridge(**kwargs: Any) -> _BridgeFixture:
    """Build a WledBridge with fake provider/mass/sendspin_server, bypassing start()."""
    call_later = Mock(return_value=Mock())
    mass = SimpleNamespace(loop=SimpleNamespace(call_later=call_later))
    provider: Any = SimpleNamespace(mass=mass)
    sendspin_server = _FakeSendspinServer()
    bridge = WledBridge(
        provider, port=11988, sendspin_server=cast("Any", sendspin_server), **kwargs
    )
    return _BridgeFixture(bridge=bridge, call_later=call_later, sendspin_server=sendspin_server)


def _set_transport(bridge: WledBridge, transport: Mock) -> None:
    """
    Attach a mock transport to the bridge.

    Cast to Any: assigning the Mock directly narrows the attribute's inferred type past
    the call boundary of a later ``await bridge.stop()``/``_render_tick()``, which makes
    mypy treat a subsequent ``is None`` assert on it (after that type has been reset back
    to ``None`` inside the real method) as unreachable.
    """
    bridge._transport = cast("Any", transport)


def _dirty_bridge_state(bridge: WledBridge) -> None:
    """Populate the bridge's cached feature state as if mid-stream."""
    bridge._latest_loudness = 12345
    bridge._latest_spectrum = [100] * SPECTRUM_BINS
    bridge._latest_f_peak_freq = 440
    bridge._latest_f_peak_amp = 5000
    bridge._peak_pending = True


class TestStreamLifecycleResetsFeatureState:
    """A seek or new stream must not leak the previous stream's feature values."""

    def test_stream_start_resets_stale_feature_state(self) -> None:
        """A new stream must not reuse the previous stream's cached values."""
        bridge = _make_bridge().bridge
        _dirty_bridge_state(bridge)
        bridge._on_stream_start()
        assert bridge._latest_loudness == 0
        assert bridge._latest_spectrum == [0] * SPECTRUM_BINS
        assert bridge._latest_f_peak_freq == 0
        assert bridge._latest_f_peak_amp == 0
        assert bridge._peak_pending is False

    def test_stream_clear_resets_stale_feature_state(self) -> None:
        """A seek must not keep sending pre-seek loudness/spectrum/peak values."""
        bridge = _make_bridge().bridge
        _dirty_bridge_state(bridge)
        bridge._on_stream_clear()
        assert bridge._latest_loudness == 0
        assert bridge._latest_spectrum == [0] * SPECTRUM_BINS
        assert bridge._latest_f_peak_freq == 0
        assert bridge._latest_f_peak_amp == 0
        assert bridge._peak_pending is False

    def test_stream_clear_also_drops_queued_frames(self) -> None:
        """A seek must discard not-yet-promoted frames from before it too."""
        bridge = _make_bridge().bridge
        bridge._is_streaming = True
        bridge._on_frame(ExtractedFrame(timestamp_us=0, loudness=1000))
        assert len(bridge._pending_frames) == 1
        bridge._on_stream_clear()
        assert len(bridge._pending_frames) == 0


class TestDrainPending:
    """Frames should only be promoted once their timestamp has passed."""

    def test_drain_promotes_frames_up_to_now(self) -> None:
        """Only frames whose timestamp has passed should update the sendable state."""
        bridge = _make_bridge().bridge
        bridge._is_streaming = True
        bridge._on_frame(
            ExtractedFrame(
                timestamp_us=1000,
                loudness=30000,
                spectrum=np.array([1] * SPECTRUM_BINS),
                f_peak_freq=440,
                f_peak_amp=2000,
                peak=200,
            )
        )
        bridge._on_frame(ExtractedFrame(timestamp_us=5000, loudness=50000))

        bridge._drain_pending(now_us=1000)
        assert bridge._latest_loudness == 30000
        assert bridge._peak_pending is True
        assert len(bridge._pending_frames) == 1  # the 5000us frame is still in the future

        bridge._drain_pending(now_us=5000)
        assert bridge._latest_loudness == 50000
        assert len(bridge._pending_frames) == 0


class TestRenderTick:
    """The fixed-rate send loop should send exactly one packet per tick and never crash it."""

    def test_sends_packet_when_transport_present(self) -> None:
        """A drained frame should be sent as a 44-byte packet on the next tick."""
        bridge = _make_bridge().bridge
        bridge._is_streaming = True
        transport = Mock()
        _set_transport(bridge, transport)
        bridge._on_frame(ExtractedFrame(timestamp_us=0, loudness=40000))

        bridge._render_tick()

        transport.sendto.assert_called_once()
        (packet,) = transport.sendto.call_args.args
        assert len(packet) == 44

    def test_does_nothing_when_not_streaming(self) -> None:
        """A tick that fires after streaming stopped must not send anything."""
        bridge = _make_bridge().bridge
        bridge._is_streaming = False
        transport = Mock()
        _set_transport(bridge, transport)

        bridge._render_tick()

        transport.sendto.assert_not_called()

    def test_send_failure_is_logged_not_raised_and_loop_reschedules(self) -> None:
        """One bad tick must not kill the loop or propagate out of the callback."""
        fixture = _make_bridge()
        fixture.bridge._is_streaming = True
        transport = Mock()
        transport.sendto.side_effect = OSError("network unreachable")
        _set_transport(fixture.bridge, transport)

        fixture.bridge._render_tick()  # must not raise

        fixture.call_later.assert_called_once()


class TestStop:
    """Stopping the bridge must not leak the UDP transport even if teardown fails partway."""

    async def test_closes_transport_even_when_remove_client_fails(self) -> None:
        """A failed client removal must not leak the UDP transport."""
        fixture = _make_bridge()
        transport = Mock()
        _set_transport(fixture.bridge, transport)
        fixture.bridge._sendspin_client = cast("Any", SimpleNamespace(client_id="wled-zone-11988"))
        fixture.sendspin_server.remove_client.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await fixture.bridge.stop()

        transport.close.assert_called_once()
        assert fixture.bridge._transport is None
        assert fixture.bridge._sendspin_client is None

    async def test_closes_transport_on_clean_shutdown(self) -> None:
        """The normal shutdown path removes the client and closes the transport."""
        fixture = _make_bridge()
        transport = Mock()
        _set_transport(fixture.bridge, transport)
        fixture.bridge._sendspin_client = cast("Any", SimpleNamespace(client_id="wled-zone-11988"))

        await fixture.bridge.stop()

        fixture.sendspin_server.remove_client.assert_awaited_once_with("wled-zone-11988")
        transport.close.assert_called_once()
        assert fixture.bridge._transport is None
