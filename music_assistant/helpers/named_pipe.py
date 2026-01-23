"""Simple async-friendly named pipe writer using threads."""

from __future__ import annotations

import asyncio
import errno as errno_module
import logging
import os
import time
from contextlib import suppress
from pathlib import Path

_LOGGER = logging.getLogger("named_pipe")


class AsyncNamedPipeWriter:
    """Async writer for named pipes."""

    def __init__(self, pipe_path: str) -> None:
        """Initialize named pipe writer."""
        self._pipe_path = pipe_path
        self._write_fd: int | None = None

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

    def _ensure_write_fd(self) -> bool:
        """Ensure we have a write fd open. Returns True if successful."""
        if self._write_fd is not None:
            return True
        if not Path(self._pipe_path).exists():
            return False
        # Retry opening until reader is available (up to 1s)
        for _ in range(20):
            try:
                self._write_fd = os.open(self._pipe_path, os.O_WRONLY | os.O_NONBLOCK)
                return True
            except OSError as e:
                if e.errno in (errno_module.ENXIO, errno_module.ENOENT):
                    time.sleep(0.05)
                    continue
                raise
        _LOGGER.warning("Could not open pipe %s: no reader after retries", self._pipe_path)
        return False

    async def write(self, data: bytes) -> None:
        """Write data to the named pipe."""

        def _write() -> None:
            if not self._ensure_write_fd():
                return
            try:
                assert self._write_fd is not None
                os.write(self._write_fd, data)
            except OSError as e:
                if e.errno == errno_module.EPIPE:
                    # Reader closed, reset fd for next attempt
                    if self._write_fd is not None:
                        with suppress(Exception):
                            os.close(self._write_fd)
                        self._write_fd = None
                else:
                    raise

        await asyncio.to_thread(_write)

    async def remove(self) -> None:
        """Close write fd and remove the pipe."""
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
