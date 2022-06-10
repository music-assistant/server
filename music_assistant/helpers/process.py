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

LOGGER = logging.getLogger(__name__)

DEFAULT_CHUNKSIZE = 128000
DEFAULT_TIMEOUT = 120


class AsyncProcess:
    """Implementation of a (truly) non blocking subprocess."""

    def __init__(
        self,
        args: Union[List, str],
        enable_write: bool = False,
        chunk_size: int = DEFAULT_CHUNKSIZE,
        use_stderr: bool = False,
    ):
        """Initialize."""
        self._proc = None
        self._args = args
        self._use_stderr = use_stderr
        self._enable_write = enable_write
        self._attached_task: asyncio.Task = None
        self.closed = False
        self.chunk_size = chunk_size or DEFAULT_CHUNKSIZE

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
                stdout=asyncio.subprocess.PIPE if not self._use_stderr else None,
                stderr=asyncio.subprocess.PIPE if self._use_stderr else None,
                limit=self.chunk_size * 5,
                close_fds=True,
            )
        else:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if self._enable_write else None,
                stdout=asyncio.subprocess.PIPE if not self._use_stderr else None,
                stderr=asyncio.subprocess.PIPE if self._use_stderr else None,
                limit=self.chunk_size * 5,
                close_fds=True,
            )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self.closed = True
        if self._attached_task:
            # cancel the attached reader/writer task
            try:
                self._attached_task.cancel()
                await self._attached_task
            except asyncio.CancelledError:
                pass
        if self._proc.returncode is None:
            # prevent subprocess deadlocking, read remaining bytes
            await self._proc.communicate(b"" if self._enable_write else None)
            if self._proc.returncode is None:
                # just in case?
                self._proc.kill()

    async def iterate_chunks(self) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the process stdout. Generator."""
        while True:
            chunk = await self._read_chunk()
            yield chunk
            if len(chunk) < self.chunk_size:
                del chunk
                break
            del chunk

    async def _read_chunk(self, timeout: int = DEFAULT_TIMEOUT) -> bytes:
        """Read chunk_size bytes from the process stdout."""
        if self.closed:
            return b""
        try:
            async with _timeout(timeout):
                return await self._proc.stdout.readexactly(self.chunk_size)
        except asyncio.IncompleteReadError as err:
            return err.partial
        except asyncio.TimeoutError:
            return b""

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self.closed:
            return
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (
            AttributeError,
            AssertionError,
            BrokenPipeError,
            RuntimeError,
            ConnectionResetError,
        ) as err:
            # already exited, race condition
            raise asyncio.CancelledError() from err

    def write_eof(self) -> None:
        """Write end of file to to process stdin."""
        if self.closed:
            return
        if self._proc.stdin.can_write_eof():
            self._proc.stdin.write_eof()

    async def communicate(self, input_data: Optional[bytes] = None) -> bytes:
        """Write bytes to process and read back results."""
        return await self._proc.communicate(input_data)

    def attach_task(self, coro: Coroutine) -> asyncio.Task:
        """Attach given coro func as reader/writer task to properly cancel it when needed."""
        self._attached_task = task = asyncio.create_task(coro)
        return task


async def check_output(shell_cmd: str) -> Tuple[int, bytes]:
    """Run shell subprocess and return output."""
    proc = await asyncio.create_subprocess_shell(
        shell_cmd,
        stderr=asyncio.subprocess.STDOUT,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return (proc.returncode, stdout)
