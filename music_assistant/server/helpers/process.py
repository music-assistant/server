"""Implementation of a (truly) non blocking subprocess.

The subprocess implementation in asyncio can (still) sometimes cause deadlocks,
even when properly handling reading/writes from different tasks.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Coroutine
from contextlib import suppress

LOGGER = logging.getLogger(__name__)

DEFAULT_CHUNKSIZE = 128000
DEFAULT_TIMEOUT = 60

# pylint: disable=invalid-name


class AsyncProcess:
    """Implementation of a (truly) non blocking subprocess."""

    def __init__(
        self,
        args: list,
        enable_stdin: bool = False,
        enable_stdout: bool = True,
        enable_stderr: bool = False,
    ):
        """Initialize."""
        self._proc = None
        self._args = args
        self._enable_stdin = enable_stdin
        self._enable_stdout = enable_stdout
        self._enable_stderr = enable_stderr
        self._attached_task: asyncio.Task = None
        self.closed = False

    async def __aenter__(self) -> AsyncProcess:
        """Enter context manager."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._args,
            stdin=asyncio.subprocess.PIPE if self._enable_stdin else None,
            stdout=asyncio.subprocess.PIPE if self._enable_stdout else None,
            stderr=asyncio.subprocess.PIPE if self._enable_stderr else None,
            close_fds=True,
        )

        # Fix BrokenPipeError due to a race condition
        # by attaching a default done callback
        def _done_cb(fut: asyncio.Future):
            fut.exception()

        self._proc._transport._protocol._stdin_closed.add_done_callback(_done_cb)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self.closed = True
        # make sure the process is cleaned up
        self.write_eof()
        if self._proc.returncode is None:
            try:
                async with asyncio.timeout(10):
                    await self._proc.communicate()
            except TimeoutError:
                self._proc.kill()
                await self._proc.communicate()
        if self._proc.returncode is None:
            self._proc.kill()
        if self._attached_task and not self._attached_task.done():
            with suppress(asyncio.CancelledError):
                self._attached_task.cancel()

    async def iter_chunked(self, n: int = DEFAULT_CHUNKSIZE) -> AsyncGenerator[bytes, None]:
        """Yield chunks of n size from the process stdout."""
        while True:
            chunk = await self.readexactly(n)
            if chunk == b"":
                break
            yield chunk
            if len(chunk) < n:
                break

    async def iter_any(self, n: int = DEFAULT_CHUNKSIZE) -> AsyncGenerator[bytes, None]:
        """Yield chunks as they come in from process stdout."""
        while True:
            chunk = await self.read(n)
            if chunk == b"":
                break
            yield chunk

    async def readexactly(self, n: int, timeout: int = DEFAULT_TIMEOUT) -> bytes:
        """Read exactly n bytes from the process stdout (or less if eof)."""
        try:
            async with asyncio.timeout(timeout):
                return await self._proc.stdout.readexactly(n)
        except asyncio.IncompleteReadError as err:
            return err.partial

    async def read(self, n: int, timeout: int = DEFAULT_TIMEOUT) -> bytes:
        """Read up to n bytes from the stdout stream.

        If n is positive, this function try to read n bytes,
        and may return less or equal bytes than requested, but at least one byte.
        If EOF was received before any byte is read, this function returns empty byte object.
        """
        async with asyncio.timeout(timeout):
            return await self._proc.stdout.read(n)

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self.closed or self._proc.stdin.is_closing():
            return
        self._proc.stdin.write(data)
        with suppress(BrokenPipeError):
            await self._proc.stdin.drain()

    def write_eof(self) -> None:
        """Write end of file to to process stdin."""
        if self.closed or self._proc.stdin.is_closing():
            return
        try:
            if self._proc.stdin.can_write_eof():
                self._proc.stdin.write_eof()
        except (
            AttributeError,
            AssertionError,
            BrokenPipeError,
            RuntimeError,
            ConnectionResetError,
        ):
            # already exited, race condition
            pass

    async def communicate(self, input_data: bytes | None = None) -> tuple[bytes, bytes]:
        """Write bytes to process and read back results."""
        return await self._proc.communicate(input_data)

    def attach_task(self, coro: Coroutine) -> asyncio.Task:
        """Attach given coro func as reader/writer task to properly cancel it when needed."""
        self._attached_task = task = asyncio.create_task(coro)
        return task


async def check_output(shell_cmd: str) -> tuple[int, bytes]:
    """Run shell subprocess and return output."""
    proc = await asyncio.create_subprocess_shell(
        shell_cmd,
        stderr=asyncio.subprocess.STDOUT,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return (proc.returncode, stdout)
