"""Tests for the AsyncProcess helper."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from music_assistant.helpers import process as process_module
from music_assistant.helpers.process import AsyncProcess

# Comfortably beyond the OS pipe capacity plus asyncio's default high-water mark,
# so the bytes are guaranteed to still be queued in our own write buffer.
_MORE_THAN_THE_PIPE_HOLDS = b"\x00" * (4 * 1024 * 1024)

# Ignores SIGINT and keeps stdout open, so nothing but SIGKILL ends it and its
# pipe never reaches EOF. The marker tells the test the handler is installed.
_WEDGED_CHILD = (
    "import signal, sys, time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
    "sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(30)"
)


@pytest.fixture(name="piped_process")
async def piped_process_fixture() -> AsyncGenerator[tuple[AsyncProcess, int]]:
    """
    Yield an AsyncProcess writing to a real pipe, plus its unread read end.

    The write side is a genuine asyncio pipe transport, so the flow control
    stdin_quiesced relies on behaves exactly as it does against a real process,
    while nothing consumes the read end until a test chooses to.
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
async def test_stdin_quiesced_waits_for_the_write_buffer_to_empty(
    piped_process: tuple[AsyncProcess, int],
) -> None:
    """The block is entered only once every queued byte has reached the reader."""
    proc, read_fd = piped_process
    _queue_without_waiting(proc, _MORE_THAN_THE_PIPE_HOLDS)
    assert proc.proc is not None
    assert proc.proc.stdin is not None
    assert proc.proc.stdin.transport.get_write_buffer_size() > 0
    reader = asyncio.create_task(_consume(read_fd, len(_MORE_THAN_THE_PIPE_HOLDS)))

    async with proc.stdin_quiesced() as quiesced:
        assert quiesced is True
        assert proc.proc.stdin.transport.get_write_buffer_size() == 0

    await reader


@pytest.mark.asyncio
async def test_stdin_quiesced_reports_a_reader_that_never_catches_up(
    piped_process: tuple[AsyncProcess, int],
) -> None:
    """A reader that never consumes the pipe reports failure instead of hanging."""
    proc, _ = piped_process
    _queue_without_waiting(proc, _MORE_THAN_THE_PIPE_HOLDS)

    async with proc.stdin_quiesced(timeout=0.2) as quiesced:
        assert quiesced is False


@pytest.mark.asyncio
async def test_stdin_quiesced_keeps_writes_out_of_the_block(
    piped_process: tuple[AsyncProcess, int],
) -> None:
    """
    No write can land while the block runs, so nothing queues up behind a drain.

    This is the guarantee the block exists for: a caller telling the process
    something about the bytes it has been handed needs stdin to stay as it left it
    until the process has answered.
    """
    proc, read_fd = piped_process
    reader = asyncio.create_task(_consume(read_fd, len(b"late")))

    async with proc.stdin_quiesced() as quiesced:
        assert quiesced is True
        writing = asyncio.create_task(proc.write(b"late"))
        await asyncio.sleep(0)

        assert not writing.done()
        assert proc.proc is not None
        assert proc.proc.stdin is not None
        assert proc.proc.stdin.transport.get_write_buffer_size() == 0

    await writing
    await reader


@pytest.mark.asyncio
async def test_stdin_quiesced_restores_normal_writing(
    piped_process: tuple[AsyncProcess, int],
) -> None:
    """Writing carries on unaffected afterwards, on the restored buffer limits."""
    proc, read_fd = piped_process
    assert proc.proc is not None
    assert proc.proc.stdin is not None
    limits = proc.proc.stdin.transport.get_write_buffer_limits()
    reader = asyncio.create_task(_consume(read_fd, len(b"first") + len(b"second")))
    await proc.write(b"first")

    async with proc.stdin_quiesced() as quiesced:
        assert quiesced is True

    assert proc.proc.stdin.transport.get_write_buffer_limits() == limits
    await proc.write(b"second")
    await reader


@pytest.mark.asyncio
async def test_stdin_quiesced_is_a_noop_without_a_process() -> None:
    """A process that was never started quiesces trivially rather than raising."""
    proc = AsyncProcess(["cat"], stdin=True)

    async with proc.stdin_quiesced() as quiesced:
        assert quiesced is True


@pytest.mark.asyncio
async def test_iter_stdout_drains_lines_buffered_after_exit() -> None:
    """
    Output written just before the process exits is still delivered.

    A short-lived process can write everything and be reaped before the reader
    runs, so keying the stdout reader off the returncode would drop exactly the
    output that explains why it exited.
    """
    proc = AsyncProcess(
        ["sh", "-c", "for i in $(seq 1 50); do echo line$i; done"],
        stdout=True,
        stderr=asyncio.subprocess.STDOUT,
    )
    await proc.start()
    await proc.wait()

    lines = [line async for line in proc.iter_stdout()]

    assert lines == [f"line{index}" for index in range(1, 51)]
    await proc.close()


@pytest.mark.asyncio
async def test_read_stdout_stops_once_the_process_is_closed() -> None:
    """A closed process reports EOF instead of waiting on a stream it no longer owns."""
    proc = AsyncProcess(["sh", "-c", "sleep 30"], stdout=True, stderr=asyncio.subprocess.STDOUT)
    await proc.start()
    await proc.close()

    assert await proc.read_stdout() == b""


@pytest.mark.asyncio
async def test_second_close_returns_without_waiting_out_the_stream_locks() -> None:
    """
    Closing an already-closed process is cheap.

    close() keeps the stdin/stdout locks it takes, so a second call used to sit
    through both 5s acquire timeouts - a delay paid on every supervised restart
    that closes the process before its own cleanup runs.
    """
    proc = AsyncProcess(["sh", "-c", "sleep 30"], stdout=True, stderr=asyncio.subprocess.STDOUT)
    await proc.start()
    await proc.close()

    started = time.monotonic()
    await proc.close()

    assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_kill_retrieves_the_exception_of_a_finished_stdin_feeder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A stdin feeder that already failed is awaited and its failure logged.

    Awaiting only a still-pending task leaves the exception of one that already
    ended unretrieved, which asyncio reports as unhandled when it is collected;
    retrieving it without logging would drop the only trace of the failure.
    """

    async def _failing_feeder() -> None:
        raise RuntimeError("feeder blew up")

    proc = AsyncProcess(["sh", "-c", "sleep 30"], stdout=True, stderr=asyncio.subprocess.STDOUT)
    await proc.start()
    feeder = asyncio.create_task(_failing_feeder())
    await asyncio.wait([feeder])  # let it fail without retrieving the exception
    proc._stdin_feeder_task = feeder

    await proc.kill()

    # asyncio clears this flag once the exception has been retrieved; while it is
    # set the task is the one that triggers "Task exception was never retrieved"
    assert feeder._log_traceback is False
    assert "feeder blew up" in caplog.text


@pytest.mark.asyncio
async def test_close_reaps_a_child_that_never_closes_its_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A child holding its pipes open must not keep close() from reaping it.

    Draining stdout is what lets a healthy process flush before it is reaped, so
    an unbounded drain waits out a wedged child forever and the terminate/SIGKILL
    escalation is never reached.
    """
    monkeypatch.setattr(process_module, "PIPE_DRAIN_TIMEOUT", 0.2)
    proc = AsyncProcess(
        [sys.executable, "-c", _WEDGED_CHILD], stdout=True, stderr=asyncio.subprocess.STDOUT
    )
    await proc.start()
    assert await proc.read_stdout() == b"ready\n"

    async with asyncio.timeout(20):
        await proc.close()

    assert proc.returncode is not None
