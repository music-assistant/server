"""
Implementation of a (truly) non blocking subprocess.

The subprocess implementation in asyncio can (still) sometimes cause deadlocks,
even when properly handling reading/writes from different tasks.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator, Coroutine, List, Optional, Tuple, Union

from async_timeout import timeout as _timeout

LOGGER = logging.getLogger("AsyncProcess")

DEFAULT_CHUNKSIZE = 64000
DEFAULT_TIMEOUT = 120


class AsyncProcess:
    """Implementation of a (truly) non blocking subprocess."""

    def __init__(self, args: Union[List, str], enable_write: bool = False):
        """Initialize."""
        self._proc = None
        self._args = args
        self._enable_write = enable_write
        self._attached_task: asyncio.Task = None
        self.closed = False

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
        self.closed = True
        if self._attached_task:
            # cancel the attached reader/writer task
            self._attached_task.cancel()
        if self._proc.returncode is None:
            # prevent subprocess deadlocking, send terminate and read remaining bytes
            try:
                self._proc.terminate()
                # close stdin and let it drain
                if self._enable_write:
                    await self._proc.stdin.drain()
                    self._proc.stdin.close()
                # read remaining bytes
                await self._proc.stdout.read()
                # we really want to make this thing die ;-)
                self._proc.kill()
            except (
                ProcessLookupError,
                BrokenPipeError,
                RuntimeError,
                ConnectionResetError,
            ):
                pass
        del self._proc

    async def iterate_chunks(
        self, chunk_size: int = DEFAULT_CHUNKSIZE, timeout: int = DEFAULT_TIMEOUT
    ) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the process stdout. Generator."""
        while True:
            chunk = await self.read(chunk_size, timeout)
            if not chunk:
                break
            yield chunk
            if chunk_size is not None and len(chunk) < chunk_size:
                break

    async def read(
        self, chunk_size: int = DEFAULT_CHUNKSIZE, timeout: int = DEFAULT_TIMEOUT
    ) -> bytes:
        """Read x bytes from the process stdout."""
        try:
            async with _timeout(timeout):
                if chunk_size is None:
                    return await self._proc.stdout.read(DEFAULT_CHUNKSIZE)
                return await self._proc.stdout.readexactly(chunk_size)
        except asyncio.IncompleteReadError as err:
            return err.partial
        except AttributeError as exc:
            raise asyncio.CancelledError() from exc
        except asyncio.TimeoutError:
            return b""

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self.closed:
            return
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (AttributeError, AssertionError, BrokenPipeError):
            # already exited, race condition
            pass

    def write_eof(self) -> None:
        """Write end of file to to process stdin."""
        try:
            if self._proc.stdin.can_write_eof():
                self._proc.stdin.write_eof()
        except (AttributeError, AssertionError, BrokenPipeError):
            # already exited, race condition
            pass

    async def communicate(self, input_data: Optional[bytes] = None) -> bytes:
        """Write bytes to process and read back results."""
        return await self._proc.communicate(input_data)

    def attach_task(self, coro: Coroutine) -> None:
        """Attach given coro func as reader/writer task to properly cancel it when needed."""
        self._attached_task = asyncio.create_task(coro)


async def check_output(shell_cmd: str) -> Tuple[int, bytes]:
    """Run shell subprocess and return output."""
    proc = await asyncio.create_subprocess_shell(
        shell_cmd,
        stderr=asyncio.subprocess.STDOUT,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return (proc.returncode, stdout)
