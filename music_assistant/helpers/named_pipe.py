"""Simple async-friendly named pipe writer using threads."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
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

    @property
    def path(self) -> str:
        """Return the named pipe path."""
        return self._pipe_path

    async def create(self) -> None:
        """Create the named pipe (if it does not exist)."""

        def _create() -> None:
            with suppress(FileExistsError):
                os.mkfifo(self._pipe_path)

        await asyncio.to_thread(_create)

    async def write(self, data: bytes, log_slow_writes: bool = True) -> None:
        """Write data to the named pipe (blocking operation runs in thread).

        Args:
            data: Data to write to the pipe
            log_slow_writes: Whether to log slow writes (>5s)

        Raises:
            RuntimeError: If pipe is not open
        """
        start_time = time.time()

        def _write() -> None:
            with open(self._pipe_path, "wb") as pipe_file:
                pipe_file.write(data)

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

    async def remove(self) -> None:
        """Remove the named pipe."""

        def _remove() -> None:
            with suppress(Exception):
                os.remove(self._pipe_path)

        await asyncio.to_thread(_remove)

    def __str__(self) -> str:
        """Return string representation."""
        return self._pipe_path
