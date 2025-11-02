"""Simple async-friendly named pipe writer using threads."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logging import Logger


class AsyncNamedPipeWriter:
    """Simple async writer for named pipes using thread pool for blocking I/O."""

    def __init__(self, pipe_path: str, logger: Logger | None = None) -> None:
        """Initialize named pipe writer.

        Args:
            pipe_path: Path to the named pipe
            logger: Optional logger for debug/error messages
        """
        self._pipe_path = pipe_path
        self.logger = logger
        self._fd: int | None = None

    @property
    def path(self) -> str:
        """Return the named pipe path."""
        return self._pipe_path

    @property
    def is_open(self) -> bool:
        """Return True if the pipe is currently open."""
        return self._fd is not None

    async def create(self) -> None:
        """Create the named pipe (if it does not exist)."""

        def _create() -> None:
            with suppress(FileExistsError):
                os.mkfifo(self._pipe_path, 0o600)

        await asyncio.to_thread(_create)

    async def open(self) -> None:
        """Open the named pipe for writing (blocking operation runs in thread)."""
        if self._fd is not None:
            return  # Already open

        def _open() -> int:
            # Open pipe in BLOCKING write mode (simple approach)
            # - open() blocks until the reader (cliraop) connects
            # - write() blocks if pipe buffer is full
            # Both operations run in thread pool via asyncio.to_thread(),
            # so they won't block the event loop
            return os.open(self._pipe_path, os.O_WRONLY)

        self._fd = await asyncio.to_thread(_open)

        if self.logger:
            self.logger.debug("Pipe %s opened for writing", self._pipe_path)

    async def write(self, data: bytes, log_slow_writes: bool = True) -> None:
        """Write data to the named pipe (blocking operation runs in thread).

        Args:
            data: Data to write to the pipe
            log_slow_writes: Whether to log slow writes (>5s)

        Raises:
            RuntimeError: If pipe is not open
        """
        if self._fd is None:
            raise RuntimeError(f"Pipe {self._pipe_path} is not open")

        start_time = time.time()

        def _write() -> None:
            assert self._fd is not None
            # Write all data (may block if pipe buffer is full)
            bytes_written = 0
            while bytes_written < len(data):
                n = os.write(self._fd, data[bytes_written:])
                bytes_written += n

        # Run blocking write in thread pool
        await asyncio.to_thread(_write)

        if log_slow_writes:
            elapsed = time.time() - start_time
            # Only log if it took more than 5 seconds (real stall)
            if elapsed > 5.0 and self.logger:
                self.logger.error(
                    "!!! STALLED PIPE WRITE: Took %.3fs to write %d bytes to %s",
                    elapsed,
                    len(data),
                    self._pipe_path,
                )

    async def close(self) -> None:
        """Close the named pipe."""
        if self._fd is None:
            return

        fd = self._fd
        self._fd = None

        def _close() -> None:
            with suppress(Exception):
                os.close(fd)

        await asyncio.to_thread(_close)

    async def remove(self) -> None:
        """Remove the named pipe."""
        await self.close()

        def _remove() -> None:
            with suppress(Exception):
                os.remove(self._pipe_path)

        await asyncio.to_thread(_remove)

    async def __aenter__(self) -> AsyncNamedPipeWriter:
        """Context manager entry."""
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Context manager exit."""
        await self.close()

    def __str__(self) -> str:
        """Return string representation."""
        return self._pipe_path
