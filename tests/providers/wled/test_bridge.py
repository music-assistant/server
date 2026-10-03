"""Tests for the WLED sync-zone bridge."""

from __future__ import annotations

import struct
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from aiosendspin.server.roles.visualizer.features import ExtractedFrame

from music_assistant.providers.wled.bridge import WledBridge
from music_assistant.providers.wled.constants import SPECTRUM_BINS, ScalingMode


def _make_bridge() -> tuple[WledBridge, Mock, Mock]:
    call_later = Mock(return_value=Mock())
    mass = SimpleNamespace(
        loop=SimpleNamespace(call_later=call_later, create_datagram_endpoint=AsyncMock()),
    )
    provider: Any = SimpleNamespace(mass=mass, logger=Mock())
    server = SimpleNamespace(
        clock=SimpleNamespace(now_us=lambda: 1_000_000),
        register_external_player=Mock(),
        remove_client=AsyncMock(),
    )
    sendspin_provider: Any = SimpleNamespace(server_api=server, register_bridge_player_type=Mock())
    bridge = WledBridge(
        provider,
        sendspin_provider,
        11988,
        gain_db=0.0,
        scaling_mode=ScalingMode.LINEAR,
        latency_ms=0,
    )
    return bridge, call_later, server.remove_client


def _frame(timestamp_us: int, loudness: int = 40000, peak: int | None = None) -> ExtractedFrame:
    return ExtractedFrame(
        timestamp_us=timestamp_us,
        loudness=loudness,
        spectrum=np.array([1000] * SPECTRUM_BINS),
        f_peak_freq=440,
        f_peak_amp=5000,
        peak=peak,
    )


async def test_start_opens_transport_before_registering_client() -> None:
    """The UDP transport opens first, so a failure leaves no Sendspin client behind."""
    bridge, _, _ = _make_bridge()
    cast("Any", bridge.mass.loop).create_datagram_endpoint.side_effect = OSError("bind failed")
    with pytest.raises(OSError, match="bind failed"):
        await bridge.start()
    cast("Any", bridge.sendspin_provider).register_bridge_player_type.assert_not_called()


async def test_stop_removes_client_and_closes_transport() -> None:
    """Stopping unregisters from Sendspin and closes the socket."""
    bridge, _, remove_client = _make_bridge()
    transport = Mock()
    bridge._transport = transport
    bridge._sendspin_client = Mock(client_id="wled-zone-11988")
    await bridge.stop()
    remove_client.assert_awaited_once_with("wled-zone-11988")
    transport.close.assert_called_once()


def test_frames_are_only_queued_while_streaming() -> None:
    """Frames outside a stream are ignored."""
    bridge, _, _ = _make_bridge()
    bridge._on_frame(_frame(0))
    assert not bridge._pending_frames
    bridge._on_stream_start()
    bridge._on_frame(_frame(0))
    assert len(bridge._pending_frames) == 1


def test_render_only_promotes_due_frames_and_sends_packet() -> None:
    """A frame becomes sendable once the playhead reaches it, and not before."""
    bridge, call_later, _ = _make_bridge()
    transport = Mock()
    bridge._transport = transport
    bridge._on_stream_start()
    bridge._on_frame(_frame(500_000, peak=255))
    bridge._on_frame(_frame(2_000_000, loudness=60000))
    bridge._render_tick()
    assert bridge._latest_loudness == 40000
    assert len(bridge._pending_frames) == 1
    (packet,) = transport.sendto.call_args.args
    assert len(packet) == 44
    assert struct.unpack_from("<B", packet, 16)[0] == 1
    bridge._render_tick()
    assert struct.unpack_from("<B", transport.sendto.call_args.args[0], 16)[0] == 0
    assert call_later.call_count >= 2


def test_seek_and_stream_end_reset_state() -> None:
    """Seeking drops queued features; ending the stream stops the loop."""
    bridge, _, _ = _make_bridge()
    bridge._on_stream_start()
    bridge._on_frame(_frame(2_000_000))
    bridge._latest_loudness = 123
    bridge._on_stream_clear()
    assert not bridge._pending_frames
    assert bridge._latest_loudness == 0
    bridge._on_stream_end()
    assert not bridge._is_streaming
    assert bridge._render_handle is None


def test_update_settings() -> None:
    """Settings change in place."""
    bridge, _, _ = _make_bridge()
    bridge.update_settings(250, 3.0, ScalingMode.LOGARITHMIC)
    assert bridge._latency_us == 250_000
    assert bridge._gain_db == 3.0
    assert bridge._scaling_mode is ScalingMode.LOGARITHMIC
