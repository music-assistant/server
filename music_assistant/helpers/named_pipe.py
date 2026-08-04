"""Simple async-friendly named pipe reader/writer using threads."""

from __future__ import annotations

import asyncio
import errno as errno_module
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import suppress
from functools import partial
from pathlib import Path

_LOGGER = logging.getLogger("named_pipe")


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
            data_view = memoryview(data)
            total_bytes_written = 0
            try:
                assert self._write_fd is not None
                while total_bytes_written < len(data_view):
                    bytes_written = os.write(self._write_fd, data_view[total_bytes_written:])
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
                    # Reader closed, reset fd for next attempt
                    if self._write_fd is not None:
                        with suppress(Exception):
                            os.close(self._write_fd)
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
