"""
Implementation of a (truly) non blocking subprocess.

The subprocess implementation in asyncio can (still) sometimes cause deadlocks,
even when properly handling reading/writes from different tasks.
Besides that, when using multiple asyncio subprocesses, together with uvloop
things go very wrong: https://github.com/MagicStack/uvloop/issues/317

As we rely a lot on moving chunks around through subprocesses (mainly sox),
this custom implementation can be seen as a temporary solution until the main issue
in uvloop is resolved.
"""

import asyncio
import logging
import subprocess
from typing import AsyncGenerator, List, Optional

LOGGER = logging.getLogger("AsyncProcess")


class AsyncProcess:
    """Implementation of a (truly) non blocking subprocess."""

    def __init__(
        self,
        process_args: List,
        enable_write: bool = False,
        enable_shell=False,
    ):
        """Initialize."""
        self._proc = subprocess.Popen(
            process_args,
            shell=enable_shell,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE if enable_write else None,
        )
        self.loop = asyncio.get_running_loop()
        self._cancelled = False

    async def __aenter__(self) -> "AsyncProcess":
        """Enter context manager."""
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self._cancelled = True
        if await self.loop.run_in_executor(None, self._proc.poll) is None:
            # prevent subprocess deadlocking, send terminate and read remaining bytes
            await self.loop.run_in_executor(None, self._proc.kill)
            self.loop.run_in_executor(None, self.__read)
        del self._proc

    async def iterate_chunks(
        self, chunksize: int = 512000
    ) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the process stdout. Generator."""
        while True:
            chunk = await self.read(chunksize)
            if not chunk:
                break
            yield chunk

    async def read(self, chunksize: int = -1) -> bytes:
        """Read x bytes from the process stdout."""
        if self._cancelled:
            raise asyncio.CancelledError()
        return await self.loop.run_in_executor(None, self.__read, chunksize)

    def __read(self, chunksize: int = -1):
        """Try read chunk from process."""
        try:
            return self._proc.stdout.read(chunksize)
        except (BrokenPipeError, ValueError, AttributeError):
            # Process already exited
            return b""

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self._cancelled:
            raise asyncio.CancelledError()

        def __write():
            try:
                self._proc.stdin.write(data)
            except (BrokenPipeError, ValueError, AttributeError):
                # Process already exited
                pass

        await self.loop.run_in_executor(None, __write)

    async def write_eof(self) -> None:
        """Write eof to process."""
        if self._cancelled:
            raise asyncio.CancelledError()

        def __write_eof():
            try:
                self._proc.stdin.close()
            except (BrokenPipeError, ValueError, AttributeError):
                # Process already exited
                pass

        await self.loop.run_in_executor(None, __write_eof)

    async def communicate(self, input_data: Optional[bytes] = None) -> bytes:
        """Write bytes to process and read back results."""
        if self._cancelled:
            raise asyncio.CancelledError()
        stdout, _ = await self.loop.run_in_executor(
            None, self._proc.communicate, input_data
        )
        return stdout
