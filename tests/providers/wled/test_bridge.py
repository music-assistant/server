"""Tests for the WLED sync-zone bridge's stream lifecycle and send loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from aiosendspin.server.roles.visualizer.features import ExtractedFrame

import music_assistant.providers.wled.bridge as bridge_module
from music_assistant.providers.wled.bridge import WledBridge, WledBridgeManager
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
        client = Mock(client_id="wled-zone-11988")
        client.roles_by_family = Mock(return_value=[])
        self.register_external_player = Mock(return_value=client)


@dataclass
class _BridgeFixture:
    """A WledBridge plus untyped handles to its fakes, so assertions skip static typing."""

    bridge: WledBridge
    call_later: Mock = field(default_factory=lambda: Mock(return_value=Mock()))
    sendspin_server: _FakeSendspinServer = field(default_factory=_FakeSendspinServer)


def _make_bridge(**kwargs: Any) -> _BridgeFixture:
    """Build a WledBridge with fake provider/mass/sendspin_server, bypassing start()."""
    call_later = Mock(return_value=Mock())
    mass = SimpleNamespace(
        loop=SimpleNamespace(call_later=call_later, create_datagram_endpoint=AsyncMock()),
        get_provider=Mock(return_value=None),
    )
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


class TestBridgeStart:
    """Registration and transport setup, and what a failure between them leaves behind."""

    async def test_registers_a_client_and_opens_the_transport(self) -> None:
        """The happy path registers with Sendspin and opens the multicast transport."""
        fixture = _make_bridge()
        transport = Mock()
        fixture.bridge.mass.loop.create_datagram_endpoint = AsyncMock(  # type: ignore[method-assign]
            return_value=(transport, Mock())
        )

        await fixture.bridge.start()

        fixture.sendspin_server.register_external_player.assert_called_once()
        assert fixture.bridge._transport is transport

    async def test_a_failed_transport_leaves_no_registered_client_behind(self) -> None:
        """
        A bind failure after registration must be recoverable by stopping the bridge.

        start() registers the Sendspin client before it opens the UDP transport, so the
        client outlives a failure in between -- WledBridgeManager.start() stops the
        bridge for exactly this reason, and that teardown has to actually unregister it.
        """
        fixture = _make_bridge()
        fixture.bridge.mass.loop.create_datagram_endpoint = AsyncMock(  # type: ignore[method-assign]
            side_effect=OSError("address already in use")
        )

        with pytest.raises(OSError, match="address already in use"):
            await fixture.bridge.start()

        # the client is registered but the transport never opened: exactly the state the
        # manager's cleanup has to unwind
        assert fixture.bridge._sendspin_client is not None
        assert fixture.bridge._transport is None

        await fixture.bridge.stop()

        fixture.sendspin_server.remove_client.assert_awaited_once_with("wled-zone-11988")
        assert fixture.bridge._sendspin_client is None


def _make_manager(monkeypatch: pytest.MonkeyPatch, bridge: Mock) -> WledBridgeManager:
    """Return a manager whose start() builds the given (mock) bridge on a fake server."""
    provider: Any = SimpleNamespace(mass=Mock())
    manager = WledBridgeManager(provider)
    monkeypatch.setattr(bridge_module, "WledBridge", Mock(return_value=bridge))
    return manager


class TestBridgeManagerStart:
    """A half-started bridge must never be left registered or adopted by the manager."""

    async def test_failed_start_tears_the_bridge_down_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A failure partway through start() must not leak the Sendspin registration.

        start() registers the client before opening the UDP transport, so a bind
        failure in between would otherwise leave a virtual player with nothing driving
        it -- and mass only logs an exception raised from this post-load hook, so no
        unload comes to clean it up.
        """
        bridge = Mock()
        bridge.start = AsyncMock(side_effect=OSError("address already in use"))
        bridge.stop = AsyncMock()
        manager = _make_manager(monkeypatch, bridge)

        with pytest.raises(OSError, match="address already in use"):
            await manager.start(11988)

        bridge.stop.assert_awaited_once()
        assert manager._bridge is None

    async def test_cleanup_failure_does_not_mask_the_startup_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The startup error is the actionable one, so a failed cleanup must not replace it."""
        bridge = Mock()
        bridge.start = AsyncMock(side_effect=OSError("address already in use"))
        bridge.stop = AsyncMock(side_effect=RuntimeError("teardown also broke"))
        manager = _make_manager(monkeypatch, bridge)

        with pytest.raises(OSError, match="address already in use"):
            await manager.start(11988)

        assert manager._bridge is None

    async def test_successful_start_adopts_the_bridge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bridge that came up fully is kept, and start() reports success."""
        bridge = Mock()
        bridge.start = AsyncMock()
        manager = _make_manager(monkeypatch, bridge)

        assert await manager.start(11988) is True
        assert manager._bridge is bridge


class TestBridgeManagerStop:
    """Teardown must not block provider unload, but must not hide real bugs either."""

    async def test_expected_teardown_error_is_logged_not_raised(self) -> None:
        """A closing transport or an already-gone client must not break unload."""
        provider: Any = SimpleNamespace(mass=Mock())
        manager = WledBridgeManager(provider)
        bridge = Mock(port=11988)
        bridge.stop = AsyncMock(side_effect=OSError("transport already closed"))
        manager._bridge = bridge

        await manager.stop()  # must not raise

        assert manager._bridge is None

    async def test_unexpected_error_propagates_but_still_clears_the_bridge(self) -> None:
        """Anything outside the expected teardown errors is a bug worth surfacing."""
        provider: Any = SimpleNamespace(mass=Mock())
        manager = WledBridgeManager(provider)
        bridge = Mock(port=11988)
        bridge.stop = AsyncMock(side_effect=ValueError("programming error"))
        manager._bridge = bridge

        with pytest.raises(ValueError, match="programming error"):
            await manager.stop()

        assert manager._bridge is None
