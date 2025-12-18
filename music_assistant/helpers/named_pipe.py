"""Simple async-friendly named pipe writer using threads."""

from __future__ import annotations

import asyncio
import os
import stat
from contextlib import suppress


class AsyncNamedPipeWriter:
    """Simple async writer for named pipes using thread pool for blocking I/O."""

    def __init__(self, pipe_path: str) -> None:
        """Initialize named pipe writer.

        Args:
            pipe_path: Path to the named pipe
        """
        self._pipe_path = pipe_path

    @property
    def path(self) -> str:
        """Return the named pipe path."""
        return self._pipe_path

    async def create(self) -> None:
        """Create the named pipe (if it does not exist)."""

        def _create() -> None:
            try:
                os.mkfifo(self._pipe_path)
            except FileExistsError:
                # Check if existing file is actually a named pipe
                file_stat = os.stat(self._pipe_path)
                if not stat.S_ISFIFO(file_stat.st_mode):
                    # Not a FIFO - remove and recreate
                    os.remove(self._pipe_path)
                    os.mkfifo(self._pipe_path)

        await asyncio.to_thread(_create)

    async def write(self, data: bytes) -> None:
        """Write data to the named pipe (blocking operation runs in thread)."""

        def _write() -> None:
            with open(self._pipe_path, "wb") as pipe_file:
                pipe_file.write(data)

        # Run blocking write in thread pool
        await asyncio.to_thread(_write)

    async def remove(self) -> None:
        """Remove the named pipe."""

        def _remove() -> None:
            with suppress(Exception):
                os.remove(self._pipe_path)

        await asyncio.to_thread(_remove)

    def __str__(self) -> str:
        """Return string representation."""
        return self._pipe_path
