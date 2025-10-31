"""Helper for async-friendly named pipe operations."""

from __future__ import annotations

import asyncio
import fcntl
import os
from contextlib import suppress
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logging import Logger


class AsyncNamedPipeWriter:
    """Async writer for named pipes (FIFOs) with non-blocking I/O.

    Handles opening named pipes in non-blocking mode and writing data
    asynchronously using the event loop without consuming thread pool resources.
    Automatically uses optimal write chunk size based on actual pipe buffer size.
    """

    # Default pipe buffer sizes (platform-specific)
    DEFAULT_PIPE_SIZE_MACOS = 16384  # macOS default: 16KB
    DEFAULT_PIPE_SIZE_LINUX = 65536  # Linux default: 64KB

    # Target pipe buffer size (512KB for ~1+ seconds of 44.1kHz stereo audio buffering)
    TARGET_PIPE_SIZE = 524288

    def __init__(self, pipe_path: str, logger: Logger | None = None) -> None:
        """Initialize async named pipe writer.

        Args:
            pipe_path: Path to the named pipe
            logger: Optional logger for debug/error messages
        """
        self.pipe_path = pipe_path
        self.logger = logger
        self._fd: int | None = None
        self._pipe_buffer_size: int = self.DEFAULT_PIPE_SIZE_MACOS
        self._write_chunk_size: int = 8192  # Conservative default (8KB)

    @property
    def is_open(self) -> bool:
        """Return True if the pipe is currently open."""
        return self._fd is not None

    async def open(self, increase_buffer: bool = True) -> None:
        """Open the named pipe in non-blocking mode for writing.

        Args:
            increase_buffer: Whether to attempt increasing the pipe buffer size
        """
        if self._fd is not None:
            return  # Already open

        def _open() -> tuple[int, int]:
            # Open pipe in non-blocking binary write mode
            fd = os.open(self.pipe_path, os.O_WRONLY | os.O_NONBLOCK)

            actual_size = self._pipe_buffer_size  # Default

            if increase_buffer:
                try:
                    # macOS/Linux: query current pipe buffer size
                    if hasattr(fcntl, "F_GETPIPE_SZ"):
                        current_size = fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)
                        actual_size = current_size
                        if self.logger:
                            self.logger.debug(
                                "Pipe %s buffer size: %d bytes", self.pipe_path, current_size
                            )

                        # Linux only: try to set larger size if F_SETPIPE_SZ exists
                        if hasattr(fcntl, "F_SETPIPE_SZ"):
                            try:
                                fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, self.TARGET_PIPE_SIZE)
                                # Verify the new size
                                actual_size = fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)
                                if self.logger:
                                    self.logger.info(
                                        "Pipe %s buffer increased to %d bytes",
                                        self.pipe_path,
                                        actual_size,
                                    )
                            except OSError as e:
                                if self.logger:
                                    self.logger.debug("Could not increase pipe buffer size: %s", e)
                except (OSError, AttributeError) as e:
                    if self.logger:
                        self.logger.debug("Cannot query/adjust pipe buffer size: %s", e)

            return fd, actual_size

        self._fd, self._pipe_buffer_size = await asyncio.to_thread(_open)

        # Set write chunk size based on actual pipe buffer size
        # Use 1/4 of pipe buffer to avoid filling it completely in one write
        # This allows better flow control and prevents blocking
        self._write_chunk_size = max(8192, self._pipe_buffer_size // 4)

        if self.logger:
            self.logger.debug(
                "Pipe %s opened: buffer=%d bytes, write_chunk_size=%d bytes",
                self.pipe_path,
                self._pipe_buffer_size,
                self._write_chunk_size,
            )

    async def write(self, data: bytes, timeout_per_wait: float = 0.1) -> None:
        """Write data to the named pipe asynchronously.

        Writes data in chunks sized according to the pipe's buffer capacity.
        Uses non-blocking I/O with event loop (no thread pool consumption).

        Args:
            data: Data to write to the pipe
            timeout_per_wait: Timeout for each wait iteration (default: 100ms)

        Raises:
            RuntimeError: If pipe is not open
            TimeoutError: If pipe write is blocked for too long (>400 waits)
        """
        if self._fd is None:
            raise RuntimeError(f"Pipe {self.pipe_path} is not open")

        bytes_written = 0
        wait_count = 0

        while bytes_written < len(data):
            # Write up to write_chunk_size bytes at a time
            to_write = min(self._write_chunk_size, len(data) - bytes_written)
            try:
                # Try non-blocking write
                n = os.write(self._fd, data[bytes_written : bytes_written + to_write])
                bytes_written += n
                wait_count = 0  # Reset wait counter on successful write
            except BlockingIOError:
                # Pipe buffer is full, wait until writable
                wait_count += 1
                if wait_count > 400:  # Too many waits (~40+ seconds at 100ms each)
                    raise TimeoutError(
                        f"Pipe write blocked after {wait_count} waits on {self.pipe_path}"
                    )

                loop = asyncio.get_event_loop()
                future: asyncio.Future[None] = loop.create_future()
                assert self._fd is not None  # Already checked at method entry
                fd = self._fd  # Capture fd for closure

                def on_writable(
                    _loop: asyncio.AbstractEventLoop = loop,
                    _future: asyncio.Future[None] = future,
                    _fd: int = fd,
                ) -> None:
                    _loop.remove_writer(_fd)
                    if not _future.done():
                        _future.set_result(None)

                loop.add_writer(fd, on_writable)
                try:
                    await asyncio.wait_for(future, timeout=timeout_per_wait)
                except TimeoutError:
                    loop.remove_writer(fd)
                    # Continue loop - will hit wait_count limit if truly stuck

    async def close(self) -> None:
        """Close the named pipe."""
        if self._fd is None:
            return

        with suppress(Exception):
            await asyncio.to_thread(os.close, self._fd)
        self._fd = None

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
