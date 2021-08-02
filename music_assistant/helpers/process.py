"""
Implementation of a (truly) non blocking subprocess.

The subprocess implementation in asyncio can (still) sometimes cause deadlocks,
even when properly handling reading/writes from different tasks.
"""

import asyncio
import logging
from typing import AsyncGenerator, List, Optional, Union

from async_timeout import timeout

LOGGER = logging.getLogger("AsyncProcess")

DEFAULT_CHUNKSIZE = 512000
DEFAULT_TIMEOUT = 120


class AsyncProcess:
    """Implementation of a (truly) non blocking subprocess."""

    def __init__(self, args: Union[List, str], enable_write: bool = False):
        """Initialize."""
        self._proc = None
        self._args = args
        self._enable_write = enable_write

    async def __aenter__(self) -> "AsyncProcess":
        """Enter context manager."""
        if "|" in self._args:
            args = " ".join(self._args)
        else:
            args = self._args

        if isinstance(args, str):
            self._proc = await asyncio.create_subprocess_shell(
                args,
                stdin=asyncio.subprocess.PIPE if self._enable_write else None,
                stdout=asyncio.subprocess.PIPE,
                limit=DEFAULT_CHUNKSIZE,
                close_fds=True,
            )
        else:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if self._enable_write else None,
                stdout=asyncio.subprocess.PIPE,
                limit=DEFAULT_CHUNKSIZE,
                close_fds=True,
            )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        if self._proc.returncode is None:
            # prevent subprocess deadlocking, send terminate and read remaining bytes
            if self._enable_write:
                self._proc.stdin.close()
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
            if not chunk:
                break
            yield chunk
            if chunk_size is not None and len(chunk) < chunk_size:
                break

    async def read(self, chunk_size: int = DEFAULT_CHUNKSIZE) -> bytes:
        """Read x bytes from the process stdout."""
        try:
            async with timeout(DEFAULT_TIMEOUT):
                if chunk_size is None:
                    return await self._proc.stdout.read(DEFAULT_CHUNKSIZE)
                return await self._proc.stdout.readexactly(chunk_size)
        except asyncio.IncompleteReadError as err:
            return err.partial
        except AttributeError as exc:
            raise asyncio.CancelledError() from exc

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except BrokenPipeError:
            pass
        except AttributeError:
            raise asyncio.CancelledError()

    async def communicate(self, input_data: Optional[bytes] = None) -> bytes:
        """Write bytes to process and read back results."""
        return await self._proc.communicate(input_data)
