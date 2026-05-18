"""
End-to-end integration tests for the WLED Audio Sync bridge pipeline.

These wire all the WLED-side pieces (VisualizerFrame → analyzer →
encoder → UDP transport) together against a real loopback UDP listener
and assert on the actual bytes that reach the wire. The Sendspin side
(WebSocket client, scheduler) is exercised separately by test_bridge.py
unit tests so this file can stay focused on protocol correctness.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any

import pytest
from aiosendspin.models.visualizer import VisualizerFrame

from music_assistant.providers.wled_audiosync.wled_audiosync_bridge import (
    WLED_FFT_BANDS,
    WLED_V2_MAGIC_HEADER,
    WLED_V2_PACKET_SIZE,
    WledAudioAnalyzer,
    WledV2Transport,
    encode_v2,
)
from music_assistant.providers.wled_audiosync.wled_audiosync_bridge.encoder import V2_STRUCT_FORMAT

from .conftest import LoopbackUdpListener

# --- Helpers: VisualizerFrame builders + wire-packet decoder ---


def _viz_frame(
    *,
    timestamp_us: int = 0,
    loudness: int = 0,
    f_peak: int = 0,
    spectrum: list[int] | None = None,
) -> VisualizerFrame:
    """Construct a VisualizerFrame with sensible defaults for testing."""
    return VisualizerFrame(
        timestamp_us=timestamp_us,
        loudness=loudness,
        f_peak=f_peak,
        spectrum=list(spectrum) if spectrum is not None else [0] * WLED_FFT_BANDS,
    )


def _decode_packet(packet: bytes) -> dict[str, Any]:
    """Unpack a 44-byte V2 packet into a readable dict."""
    assert len(packet) == WLED_V2_PACKET_SIZE, len(packet)
    (
        header,
        _pad1,
        sample_raw,
        sample_smth,
        sample_peak,
        reserved1,
        fft_bands,
        _pad2,
        fft_magnitude,
        fft_major_peak_hz,
    ) = struct.unpack(V2_STRUCT_FORMAT, packet)
    return {
        "header": header,
        "sample_raw": sample_raw,
        "sample_smth": sample_smth,
        "sample_peak": sample_peak,
        "reserved1": reserved1,
        "fft_bands": list(fft_bands),
        "fft_magnitude": fft_magnitude,
        "fft_major_peak_hz": fft_major_peak_hz,
    }


async def _drive_frames(
    transport: WledV2Transport,
    analyzer: WledAudioAnalyzer,
    frames: list[VisualizerFrame],
) -> int:
    """Drive every frame through analyzer → encoder → transport.send.

    Returns the count of packets actually emitted (frames whose analyzer
    output was usable).
    """
    emitted = 0
    for frame in frames:
        wled_frame = analyzer.process_frame(frame)
        if wled_frame is None:
            continue
        await transport.send(encode_v2(wled_frame))
        emitted += 1
    # Yield one tick so the loopback listener processes the final datagram(s).
    await asyncio.sleep(0.02)
    return emitted


# --- Integration tests ---


async def test_analyzer_encoder_transport_emits_well_formed_v2_packets() -> None:
    """End-to-end: VisualizerFrames → analyzer → encoder → UDP → listener captures bytes."""
    listener = LoopbackUdpListener()
    host, port = await listener.start()
    transport = WledV2Transport(address=host, port=port, duplicate_transmit=False, multicast_ttl=1)
    analyzer = WledAudioAnalyzer()
    # 8 frames with rising loudness and a single bright band.
    spectrum = [0] * WLED_FFT_BANDS
    spectrum[4] = 200
    frames = [
        _viz_frame(
            timestamp_us=i * 23_000, loudness=10_000 + i * 1_000, f_peak=440, spectrum=spectrum
        )
        for i in range(8)
    ]
    try:
        emitted = await _drive_frames(transport, analyzer, frames)
    finally:
        await transport.close()
        listener.close()

    assert emitted == len(frames)
    assert len(listener.received) == len(frames), (
        f"expected {len(frames)} packets, got {len(listener.received)}"
    )
    for packet in listener.received:
        assert len(packet) == WLED_V2_PACKET_SIZE
        assert packet[:6] == WLED_V2_MAGIC_HEADER


async def test_silent_visualizer_frames_produce_zero_band_wire_packets() -> None:
    """All-zero VisualizerFrames must yield zero bands / zero magnitude on the wire."""
    listener = LoopbackUdpListener()
    host, port = await listener.start()
    transport = WledV2Transport(address=host, port=port, duplicate_transmit=False, multicast_ttl=1)
    analyzer = WledAudioAnalyzer()
    frames = [_viz_frame(timestamp_us=i * 23_000) for i in range(5)]
    try:
        await _drive_frames(transport, analyzer, frames)
    finally:
        await transport.close()
        listener.close()

    decoded = [_decode_packet(p) for p in listener.received]
    assert decoded, "no packets reached listener"
    for d in decoded:
        assert d["fft_bands"] == [0] * WLED_FFT_BANDS, (
            f"silent input should produce zero bands, got {d['fft_bands']}"
        )
        assert d["fft_magnitude"] == 0.0
        assert d["fft_major_peak_hz"] == 0.0
        assert d["sample_raw"] == 0.0


async def test_loudness_and_peak_pass_through_to_wire() -> None:
    """A VisualizerFrame with non-zero loudness / f_peak surfaces on the wire."""
    listener = LoopbackUdpListener()
    host, port = await listener.start()
    transport = WledV2Transport(address=host, port=port, duplicate_transmit=False, multicast_ttl=1)
    analyzer = WledAudioAnalyzer()
    # Pick a known spectrum that drives band 7 to mid-scale.
    spectrum = [0] * WLED_FFT_BANDS
    spectrum[7] = 180
    # Burn a few warm-up frames so the AGC envelope and smoothing have
    # converged before we sample the wire packet for assertions.
    warmup = [
        _viz_frame(timestamp_us=i * 23_000, loudness=32_000, f_peak=1_000, spectrum=spectrum)
        for i in range(10)
    ]
    try:
        await _drive_frames(transport, analyzer, warmup)
        # Now the assertion frame.
        await _drive_frames(
            transport,
            analyzer,
            [
                _viz_frame(
                    timestamp_us=11 * 23_000, loudness=32_000, f_peak=1_000, spectrum=spectrum
                )
            ],
        )
    finally:
        await transport.close()
        listener.close()

    decoded = [_decode_packet(p) for p in listener.received]
    assert decoded, "no packets reached listener"
    last = decoded[-1]
    # loudness 32_000 / 65_535 * 255 ≈ 124.5 — generous slack for smoothing.
    assert 100.0 < last["sample_raw"] < 130.0, last["sample_raw"]
    # f_peak passes through verbatim.
    assert last["fft_major_peak_hz"] == pytest.approx(1_000.0)
    # Band 7 should be the dominant band on the wire.
    bands = last["fft_bands"]
    argmax = bands.index(max(bands))
    assert argmax == 7, f"expected band 7 to be brightest, got argmax={argmax} bands={bands}"


async def test_duplicate_transmit_doubles_packet_count_on_wire() -> None:
    """Each encoded frame emits exactly two wire packets when duplicate_transmit=True."""
    listener = LoopbackUdpListener()
    host, port = await listener.start()
    transport = WledV2Transport(address=host, port=port, duplicate_transmit=True, multicast_ttl=1)
    analyzer = WledAudioAnalyzer()
    spectrum = [0] * WLED_FFT_BANDS
    spectrum[2] = 100
    n_frames = 6
    frames = [
        _viz_frame(timestamp_us=i * 23_000, loudness=20_000, f_peak=500, spectrum=spectrum)
        for i in range(n_frames)
    ]
    try:
        emitted = await _drive_frames(transport, analyzer, frames)
    finally:
        await transport.close()
        listener.close()

    assert emitted == n_frames
    assert len(listener.received) == n_frames * 2, (
        f"duplicate-tx should double packets: got {len(listener.received)} vs {n_frames * 2}"
    )
    # Adjacent packets should be byte-identical pairs.
    for i in range(0, len(listener.received), 2):
        assert listener.received[i] == listener.received[i + 1], (
            f"duplicate-tx pair at offset {i} not identical"
        )


async def test_transport_reopens_after_close_and_continues_emitting() -> None:
    """A bridge that closes its transport mid-session can reopen and emit again."""
    listener = LoopbackUdpListener()
    host, port = await listener.start()
    transport = WledV2Transport(address=host, port=port, duplicate_transmit=False, multicast_ttl=1)
    analyzer = WledAudioAnalyzer()
    spectrum = [0] * WLED_FFT_BANDS
    spectrum[5] = 150
    try:
        # Cycle 1.
        await _drive_frames(
            transport,
            analyzer,
            [
                _viz_frame(timestamp_us=i * 23_000, loudness=20_000, spectrum=spectrum)
                for i in range(3)
            ],
        )
        count_cycle1 = len(listener.received)
        assert count_cycle1 == 3
        # Simulate a destination change / restart.
        await transport.close()
        assert not transport.is_open
        # Cycle 2 — transport should reopen lazily on first send.
        await _drive_frames(
            transport,
            analyzer,
            [
                _viz_frame(timestamp_us=(10 + i) * 23_000, loudness=20_000, spectrum=spectrum)
                for i in range(3)
            ],
        )
    finally:
        await transport.close()
        listener.close()

    assert len(listener.received) == 6, (
        f"expected 6 packets across two cycles, got {len(listener.received)}"
    )
