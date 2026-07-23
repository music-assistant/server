"""Tests for SharedGroupStream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest

from music_assistant.providers.msx_bridge.provider import SharedGroupStream

if TYPE_CHECKING:
    from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider


async def _chunks(*data: bytes) -> AsyncIterator[bytes]:
    """Yield bytes chunks as an async iterator."""
    for chunk in data:
        yield chunk


async def _collect(stream: SharedGroupStream, player_id: str) -> list[bytes]:
    """Subscribe and collect all chunks into a list."""
    result = []
    async for chunk in stream.subscribe(player_id):
        result.append(chunk)
    return result


async def test_subscribe_receives_all_chunks() -> None:
    """Subscriber should receive every chunk produced."""
    stream = SharedGroupStream("g1", "uri://test")
    await stream.start(_chunks(b"a", b"b", b"c"))
    result = await _collect(stream, "tv1")
    assert result == [b"a", b"b", b"c"]


async def test_late_joiner_after_finish_does_not_hang() -> None:
    """
    subscribe() called after producer has already finished must not block indefinitely.

    Regression test for: https://github.com/music-assistant/server/pull/3123#discussion_r2842897555
    """
    stream = SharedGroupStream("g1", "uri://test")
    await stream.start(_chunks(b"x", b"y"))

    # Wait for the producer to fully finish before subscribing.
    assert stream.producer_task is not None
    await asyncio.wait_for(stream.producer_task, timeout=5.0)
    assert stream.finished is True

    # Late subscriber: must get catch-up data and exit cleanly (no hang).
    result = await asyncio.wait_for(_collect(stream, "late"), timeout=5.0)
    assert result == [b"x", b"y"]


async def test_late_joiner_with_empty_stream_does_not_hang() -> None:
    """Late joiner on a stream that produced zero chunks must also exit cleanly."""
    stream = SharedGroupStream("g1", "uri://test")
    await stream.start(_chunks())  # no chunks

    assert stream.producer_task is not None
    await asyncio.wait_for(stream.producer_task, timeout=5.0)
    assert stream.finished is True

    result = await asyncio.wait_for(_collect(stream, "late"), timeout=5.0)
    assert result == []


async def test_concurrent_subscribers_receive_live_chunks() -> None:
    """Multiple subscribers joining before stream starts all receive all chunks."""
    stream = SharedGroupStream("g1", "uri://test")

    async def slow_source() -> AsyncIterator[bytes]:
        for i in range(3):
            await asyncio.sleep(0)
            yield bytes([i])

    await stream.start(slow_source())

    results = await asyncio.gather(
        _collect(stream, "tv1"),
        _collect(stream, "tv2"),
    )
    assert results[0] == results[1]
    assert len(results[0]) == 3


async def test_concurrent_replace_yields_single_stream(provider: MSXBridgeProvider) -> None:
    """
    Concurrent replacing get_or_create_shared_stream calls must yield ONE stream.

    Without serialization, both callers pass the "existing" check while the old
    producer is being awaited, each creates its own stream, and the loser's
    ffmpeg producer is orphaned — consuming audio with zero subscribers.
    """

    async def infinite_source() -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(0.01)
            yield b"chunk"

    old = await provider.get_or_create_shared_stream("g1", "uri://old", infinite_source())
    try:
        results = await asyncio.gather(
            provider.get_or_create_shared_stream("g1", "uri://new", infinite_source()),
            provider.get_or_create_shared_stream("g1", "uri://new", infinite_source()),
        )
        assert results[0] is results[1]
        assert provider._shared_streams["g1"] is results[0]
    finally:
        await old.stop()
        for stream in {id(s): s for s in provider._shared_streams.values()}.values():
            await stream.stop()
        await asyncio.gather(
            *(s.stop() for s in results),
            return_exceptions=True,
        )


async def test_cancel_stops_subscription() -> None:
    """Cancelling a subscriber's task cleans up the subscriber registry."""

    async def infinite_source() -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(0.01)
            yield b"chunk"

    stream = SharedGroupStream("g1", "uri://test")
    await stream.start(infinite_source())

    task = asyncio.create_task(_collect(stream, "tv1"))
    await asyncio.sleep(0.05)  # let subscriber register and receive some chunks
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # After cancel, subscriber should be cleaned up
    assert "tv1" not in stream.subscribers

    # Cleanup
    if stream.producer_task:
        stream.producer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stream.producer_task
