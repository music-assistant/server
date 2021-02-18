"""
Implementation of a (truly) non blocking subprocess.

The subprocess implementation in asyncio can (still) sometimes cause deadlocks,
even when properly handling reading/writes from different tasks.
"""

import asyncio
import logging
from typing import AsyncGenerator, List, Optional

LOGGER = logging.getLogger("AsyncProcess")

DEFAULT_CHUNKSIZE = 512000


class AsyncProcess:
    """Implementation of a (truly) non blocking subprocess."""

    def __init__(self, process_args: List, enable_write: bool = False):
        """Initialize."""
        self._proc = None
        self._process_args = process_args
        self._enable_write = enable_write

    async def __aenter__(self) -> "AsyncProcess":
        """Enter context manager."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._process_args,
            stdin=asyncio.subprocess.PIPE if self._enable_write else None,
            stdout=asyncio.subprocess.PIPE
        )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        if self._proc.returncode is None:
            # prevent subprocess deadlocking, send terminate and read remaining bytes
            await self.write_eof()
            try:
                self._proc.terminate()
                await self._proc.stdout.read()
                self._proc.kill()
            except (ProcessLookupError, BrokenPipeError):
                pass
        del self._proc

    async def iterate_chunks(
        self, chunk_size: int = DEFAULT_CHUNKSIZE
    ) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the process stdout. Generator."""
        while True:
            chunk = await self.read(chunk_size)
            yield chunk
            if len(chunk) < chunk_size:
                break

    async def read(self, chunk_size: int = DEFAULT_CHUNKSIZE) -> bytes:
        """Read x bytes from the process stdout."""
        try:
            return await self._proc.stdout.readexactly(chunk_size)
        except asyncio.IncompleteReadError as err:
            return err.partial

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except BrokenPipeError:
            pass

    async def write_eof(self) -> None:
        """Write eof to process."""
        if not (self._enable_write and self._proc.stdin.can_write_eof()):
            return
        try:
            self._proc.stdin.write_eof()
        except BrokenPipeError:
            pass

    async def communicate(self, input_data: Optional[bytes] = None) -> bytes:
        """Write bytes to process and read back results."""
        return await self._proc.communicate(input_data)
