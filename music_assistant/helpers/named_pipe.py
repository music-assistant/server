"""Simple async-friendly named pipe reader/writer using threads."""

from __future__ import annotations

import asyncio
import errno as errno_module
import logging
import os
import select
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from functools import partial
from pathlib import Path

_LOGGER = logging.getLogger("named_pipe")

# How long a write waits on a full pipe buffer that the reader never drains.
WRITE_STALL_TIMEOUT = 2.0
# Upper bound for a single write, so a barely-moving reader cannot park it for minutes.
WRITE_TOTAL_TIMEOUT = 30.0
# Length of one wait slice, which caps how long a reader that left goes unnoticed.
WRITE_POLL_INTERVAL_MS = 250


class AsyncNamedPipeWriter:
    """Async writer for named pipes."""

    def __init__(self, pipe_path: str, owner_id: str | None = None) -> None:
        """
        Initialize named pipe writer.

        :param pipe_path: Filesystem path of the named pipe.
        :param owner_id: Optional identifier (e.g. player_id) included in log
            messages so silent failures can be correlated to a specific device.
        """
        self._pipe_path = pipe_path
        self._owner_id = owner_id
        self._write_fd: int | None = None
        self._write_lock = asyncio.Lock()

    @property
    def path(self) -> str:
        """Return the named pipe path."""
        return self._pipe_path

    async def create(self) -> None:
        """Create the named pipe."""

        def _create() -> None:
            pipe_path = Path(self._pipe_path)
            if pipe_path.exists():
                pipe_path.unlink()
            os.mkfifo(self._pipe_path)

        await asyncio.to_thread(_create)

    async def wait_for_reader(self, timeout: float) -> bool:
        """
        Wait until the pipe has a reader attached, so writes are no longer dropped.

        A pipe without a reader accepts nothing, so a writer that is spawning its
        reader alongside itself waits here before its first write.

        :param timeout: Maximum time to wait for the reader in seconds.
        :return: True once the pipe can be written to, False if no reader
            attached before the timeout.
        """
        try:
            async with asyncio.timeout(timeout):
                while True:
                    # a concurrent write opens the same descriptor from its worker thread
                    async with self._write_lock:
                        if self._ensure_write_fd():
                            return True
                    await asyncio.sleep(0.05)
        except TimeoutError:
            return False

    async def write(self, data: bytes) -> bool:
        """
        Write data to the named pipe.

        :param data: Data to write.
        :return: True for a complete write, False when no reader is available,
            the reader closes, or the write cannot make progress.
        :raises OSError: If writing fails for another reason.
        """

        def _write() -> bool:
            if not self._ensure_write_fd():
                _LOGGER.debug(
                    "Named pipe write failed: no writable fd for pipe %s (owner=%s, %d bytes dropped)",
                    self._pipe_path,
                    self._log_owner,
                    len(data),
                )
                return False
            # hold on to the descriptor a concurrent remove() may swap out from under us
            write_fd = self._write_fd
            if write_fd is None:
                return False
            data_view = memoryview(data)
            total_bytes_written = 0
            give_up_at = time.monotonic() + WRITE_TOTAL_TIMEOUT
            stall_ends_at: float | None = None
            try:
                while total_bytes_written < len(data_view):
                    # a cancelled write releases the lock but keeps this thread going,
                    # so stop once remove() has taken the descriptor
                    if self._write_fd != write_fd:
                        return False
                    try:
                        bytes_written = os.write(write_fd, data_view[total_bytes_written:])
                    except BlockingIOError:
                        # A full buffer means the reader is behind, not gone, so wait for it
                        # to drain and try again. Only the write itself tells the two apart:
                        # a full pipe whose reader left is not reported as broken everywhere,
                        # so the wait is sliced rather than trusted to end on its own.
                        now = time.monotonic()
                        if stall_ends_at is None:
                            stall_ends_at = now + WRITE_STALL_TIMEOUT
                        if now < stall_ends_at and now < give_up_at:
                            self._wait_writable(write_fd)
                            continue
                        _LOGGER.debug(
                            "Named pipe write stalled on %s "
                            "(owner=%s, %d of %d bytes written): reader is not draining",
                            self._pipe_path,
                            self._log_owner,
                            total_bytes_written,
                            len(data),
                        )
                        return False
                    stall_ends_at = None
                    if bytes_written == 0:
                        _LOGGER.debug(
                            "Named pipe write made no progress on %s "
                            "(owner=%s, %d of %d bytes written)",
                            self._pipe_path,
                            self._log_owner,
                            total_bytes_written,
                            len(data),
                        )
                        return False
                    total_bytes_written += bytes_written
                return True
            except OSError as e:
                if e.errno == errno_module.EPIPE:
                    # Reader closed, reset fd for next attempt. A concurrent remove()
                    # may already have replaced it, and then owns it instead.
                    if self._write_fd == write_fd:
                        with suppress(Exception):
                            os.close(write_fd)
                        self._write_fd = None
                    _LOGGER.debug(
                        "Named pipe write failed (EPIPE) on %s "
                        "(owner=%s, %d of %d bytes written): reader closed",
                        self._pipe_path,
                        self._log_owner,
                        total_bytes_written,
                        len(data),
                    )
                    return False
                raise

        async with self._write_lock:
            return await asyncio.to_thread(_write)

    async def remove(self) -> None:
        """Close write fd and remove the pipe."""
        # the lock keeps a write in flight on its worker thread from reopening
        # the descriptor between the close and the unlink
        async with self._write_lock:
            if self._write_fd is not None:
                with suppress(Exception):
                    os.close(self._write_fd)
                self._write_fd = None
            pipe_path = Path(self._pipe_path)
            if pipe_path.exists():
                with suppress(Exception):
                    pipe_path.unlink()

    def __str__(self) -> str:
        """Return string representation."""
        return self._pipe_path

    @property
    def _log_owner(self) -> str:
        """Return a short descriptor for logging (owner_id or pipe path)."""
        return self._owner_id or self._pipe_path

    def _wait_writable(self, write_fd: int) -> None:
        """
        Wait a short while for the pipe to accept data again.

        :param write_fd: Descriptor of the pipe's write end.
        """
        # poll() rather than select(), which rejects a descriptor of 1024 or above
        poller = select.poll()
        poller.register(write_fd, select.POLLOUT)
        poller.poll(WRITE_POLL_INTERVAL_MS)

    def _ensure_write_fd(self) -> bool:
        """Open the write end while a reader is attached. Returns True if successful."""
        if self._write_fd is not None:
            return True
        if not Path(self._pipe_path).exists():
            return False
        try:
            self._write_fd = os.open(self._pipe_path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno in (errno_module.ENXIO, errno_module.ENOENT):
                return False
            raise
        return True


async def read_named_pipe(
    pipe_path: str,
    chunk_size: int = 4096,
) -> AsyncGenerator[bytes]:
    """
    Read raw bytes from a named pipe (FIFO) as an async generator.

    Suspends while the upstream writer is idle and transparently reopens the
    pipe on writer disconnect so an external-process restart doesn't tear down
    the consumer.

    :param pipe_path: Filesystem path of the named pipe.
    :param chunk_size: Maximum bytes returned per yield.
    """
    loop = asyncio.get_running_loop()
    while True:
        fd = os.open(pipe_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            pipe_file = os.fdopen(fd, "rb", buffering=0)
        except OSError:
            os.close(fd)
            raise
        # Small StreamReader limit so back-pressure kicks in quickly when the
        # producer writes faster than realtime (e.g. librespot's pipe backend
        # which is not natively rate-limited). asyncio's default is 64 KiB.
        # 32 KiB caps the in-flight backlog at ~180 ms at 44.1 kHz s16 stereo
        # without being so tight it risks dropping packets from realtime-paced
        # producers (shairport-sync etc.) under brief consumer-side jitter.
        reader = asyncio.StreamReader(limit=32768)
        try:
            transport, _ = await loop.connect_read_pipe(
                partial(asyncio.StreamReaderProtocol, reader),
                pipe_file,
            )
        except BaseException:
            pipe_file.close()
            raise
        try:
            while True:
                data = await reader.read(chunk_size)
                if not data:
                    break
                yield data
        finally:
            transport.close()
        # avoid a tight reopen loop when no writer is present
        await asyncio.sleep(0.1)
