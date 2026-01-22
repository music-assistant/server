"""Simple async-friendly named pipe writer using threads."""

from __future__ import annotations

import asyncio
import os
import stat
from contextlib import suppress
from pathlib import Path


class AsyncNamedPipeWriter:
    """Simple async writer for named pipes using thread pool for blocking I/O."""

    def __init__(self, pipe_path: str) -> None:
        """Initialize named pipe writer.

        Args:
            pipe_path: Path to the named pipe
        """
        self._pipe_path = pipe_path
        self._reader_fd: int | None = None

    @property
    def path(self) -> str:
        """Return the named pipe path."""
        return self._pipe_path

    async def create(self) -> None:
        """Create the named pipe (if it does not exist).

        Also opens a non-blocking reader fd to allow writers to open the pipe
        without blocking. This is needed because FIFO semantics require a reader
        to be present before a writer can open the pipe for writing.
        """

        def _create() -> None:
            try:
                os.mkfifo(self._pipe_path)
            except FileExistsError:
                # Check if existing file is actually a named pipe
                file_stat = os.stat(self._pipe_path)
                if not stat.S_ISFIFO(file_stat.st_mode):
                    # Not a FIFO - remove and recreate
                    Path(self._pipe_path).unlink()
                    os.mkfifo(self._pipe_path)

        await asyncio.to_thread(_create)
        # Open a non-blocking reader fd to allow writers to open the pipe
        # This fd will be kept open until remove() is called
        self._reader_fd = os.open(self._pipe_path, os.O_RDONLY | os.O_NONBLOCK)

    async def write(self, data: bytes) -> None:
        """Write data to the named pipe (blocking operation runs in thread)."""

        def _write() -> None:
            with open(self._pipe_path, "wb") as pipe_file:
                pipe_file.write(data)

        # Run blocking write in thread pool
        await asyncio.to_thread(_write)

    async def remove(self) -> None:
        """Remove the named pipe."""
        # Close the reader fd if it's open
        if self._reader_fd is not None:
            with suppress(Exception):
                os.close(self._reader_fd)
            self._reader_fd = None

        def _remove() -> None:
            with suppress(Exception):
                Path(self._pipe_path).unlink()

        await asyncio.to_thread(_remove)

    def __str__(self) -> str:
        """Return string representation."""
        return self._pipe_path
