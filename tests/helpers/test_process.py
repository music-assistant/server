"""Tests for the AsyncProcess helper."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from music_assistant.helpers.process import AsyncProcess

# Comfortably beyond the OS pipe capacity plus asyncio's default high-water mark,
# so the bytes are guaranteed to still be queued in our own write buffer.
_MORE_THAN_THE_PIPE_HOLDS = b"\x00" * (4 * 1024 * 1024)


@pytest.fixture(name="piped_process")
async def piped_process_fixture() -> AsyncGenerator[tuple[AsyncProcess, int]]:
    """
    Yield an AsyncProcess writing to a real pipe, plus its unread read end.

    The write side is a genuine asyncio pipe transport, so the flow control
    drain_stdin relies on behaves exactly as it does against a real process, while
    nothing consumes the read end until a test chooses to.
    """
    read_fd, write_fd = os.pipe()
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(write_fd, "wb", 0)
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    proc = AsyncProcess(["cat"], stdin=True)
    proc.proc = MagicMock(stdin=writer)
    try:
        yield proc, read_fd
    finally:
        transport.abort()
        # abort() only schedules the callback that closes the write side, so let
        # it run before the loop goes away rather than relying on the ordering.
        await asyncio.sleep(0)
        os.close(read_fd)


def _queue_without_waiting(proc: AsyncProcess, data: bytes) -> None:
    """
    Hand data to stdin without awaiting it, leaving it in the write buffer.

    ``write`` would block on its own drain while the pipe is unread, which is the
    state these tests need to set up rather than something to wait through.
    """
    assert proc.proc is not None
    assert proc.proc.stdin is not None
    proc.proc.stdin.write(data)


async def _consume(read_fd: int, total: int) -> None:
    """Read `total` bytes off the pipe so the write buffer can empty."""
    remaining = total
    while remaining > 0:
        remaining -= len(await asyncio.to_thread(os.read, read_fd, 65536))


@pytest.mark.asyncio
async def test_drain_stdin_waits_for_the_write_buffer_to_empty(
    piped_process: tuple[AsyncProcess, int],
) -> None:
    """The drain resolves only once every queued byte has reached the reader."""
    proc, read_fd = piped_process
    _queue_without_waiting(proc, _MORE_THAN_THE_PIPE_HOLDS)
    assert proc.proc is not None
    assert proc.proc.stdin is not None
    assert proc.proc.stdin.transport.get_write_buffer_size() > 0
    reader = asyncio.create_task(_consume(read_fd, len(_MORE_THAN_THE_PIPE_HOLDS)))

    assert await proc.drain_stdin() is True

    assert proc.proc.stdin.transport.get_write_buffer_size() == 0
    await reader


@pytest.mark.asyncio
async def test_drain_stdin_reports_a_reader_that_never_catches_up(
    piped_process: tuple[AsyncProcess, int],
) -> None:
    """A reader that never consumes the pipe fails the drain instead of hanging."""
    proc, _ = piped_process
    _queue_without_waiting(proc, _MORE_THAN_THE_PIPE_HOLDS)

    assert await proc.drain_stdin(timeout=0.2) is False


@pytest.mark.asyncio
async def test_drain_stdin_restores_normal_writing(
    piped_process: tuple[AsyncProcess, int],
) -> None:
    """Writing carries on unaffected after a drain, on the restored buffer limits."""
    proc, read_fd = piped_process
    reader = asyncio.create_task(_consume(read_fd, len(b"first") + len(b"second")))
    await proc.write(b"first")
    assert await proc.drain_stdin() is True

    await proc.write(b"second")

    assert await proc.drain_stdin() is True
    await reader


@pytest.mark.asyncio
async def test_drain_stdin_is_a_noop_without_a_process() -> None:
    """A drain on a process that was never started succeeds rather than raising."""
    proc = AsyncProcess(["cat"], stdin=True)

    assert await proc.drain_stdin() is True
