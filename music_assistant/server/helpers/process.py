"""
AsyncProcess.

Wrapper around asyncio subprocess to help with using pipe streams and
taking care of properly closing the process in case of exit (on both success and failures),
without deadlocking.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

LOGGER = logging.getLogger(__name__)

DEFAULT_CHUNKSIZE = 128000

# pylint: disable=invalid-name


class AsyncProcess:
    """
    AsyncProcess.

    Wrapper around asyncio subprocess to help with using pipe streams and
    taking care of properly closing the process in case of exit (on both success and failures),
    without deadlocking.
    """

    def __init__(
        self,
        args: list[str],
        enable_stdin: bool = False,
        enable_stdout: bool = True,
        enable_stderr: bool = False,
    ) -> None:
        """Initialize AsyncProcess."""
        self.proc: asyncio.subprocess.Process | None = None
        self._args = args
        self._enable_stdin = enable_stdin
        self._enable_stdout = enable_stdout
        self._enable_stderr = enable_stderr
        self._close_called = False
        self._stdin_lock = asyncio.Lock()
        self._stdout_lock = asyncio.Lock()
        self._stderr_lock = asyncio.Lock()
        self._returncode: bool | None = None

    @property
    def closed(self) -> bool:
        """Return if the process was closed."""
        return self._close_called or self.returncode is not None

    @property
    def returncode(self) -> int | None:
        """Return the erturncode of the process."""
        if self._returncode is not None:
            return self._returncode
        if self.proc is None:
            return None
        return self.proc.returncode

    async def __aenter__(self) -> AsyncProcess:
        """Enter context manager."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit context manager."""
        await self.close()
        self._returncode = self.returncode
        del self.proc
        del self._stdin_lock
        del self._stdout_lock
        del self._returncode

    async def start(self) -> None:
        """Perform Async init of process."""
        self.proc = await asyncio.create_subprocess_exec(
            *self._args,
            stdin=asyncio.subprocess.PIPE if self._enable_stdin else None,
            stdout=asyncio.subprocess.PIPE if self._enable_stdout else None,
            stderr=asyncio.subprocess.PIPE if self._enable_stderr else None,
            close_fds=True,
        )
        proc_name_simple = self._args[0].split(os.sep)[-1]
        LOGGER.debug("Started %s with PID %s", proc_name_simple, self.proc.pid)

    async def iter_chunked(self, n: int = DEFAULT_CHUNKSIZE) -> AsyncGenerator[bytes, None]:
        """Yield chunks of n size from the process stdout."""
        while True:
            chunk = await self.readexactly(n)
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

    async def readexactly(self, n: int) -> bytes:
        """Read exactly n bytes from the process stdout (or less if eof)."""
        if self.closed:
            return b""
        try:
            async with self._stdout_lock:
                return await self.proc.stdout.readexactly(n)
        except asyncio.IncompleteReadError as err:
            return err.partial

    async def read(self, n: int) -> bytes:
        """Read up to n bytes from the stdout stream.

        If n is positive, this function try to read n bytes,
        and may return less or equal bytes than requested, but at least one byte.
        If EOF was received before any byte is read, this function returns empty byte object.
        """
        if self.closed:
            return b""
        async with self._stdout_lock:
            return await self.proc.stdout.read(n)

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self.closed or self.proc.stdin.is_closing():
            return
        async with self._stdin_lock:
            self.proc.stdin.write(data)
            if self.closed or self.proc.stdin.is_closing():
                return
            with suppress(BrokenPipeError):
                await self.proc.stdin.drain()

    async def write_eof(self) -> None:
        """Write end of file to to process stdin."""
        if not self._enable_stdin:
            return
        if self.closed or self.proc.stdin.is_closing():
            return
        try:
            async with self._stdin_lock:
                if self.proc.stdin.can_write_eof():
                    self.proc.stdin.write_eof()
        except (
            AttributeError,
            AssertionError,
            BrokenPipeError,
            RuntimeError,
            ConnectionResetError,
        ):
            # already exited, race condition
            pass

    async def close(self) -> int:
        """Close/terminate the process and wait for exit."""
        self._close_called = True
        if self.proc.returncode is None:
            # make sure the process is cleaned up
            try:
                # we need to use communicate to ensure buffers are flushed
                await asyncio.wait_for(self.proc.communicate(), 5)
            except TimeoutError:
                LOGGER.debug(
                    "Process with PID %s did not stop within 5 seconds. Sending terminate...",
                    self.proc.pid,
                )
                self.proc.terminate()
                await self.proc.communicate()
        LOGGER.debug(
            "Process with PID %s stopped with returncode %s", self.proc.pid, self.proc.returncode
        )
        return self.proc.returncode

    async def wait(self) -> int:
        """Wait for the process and return the returncode."""
        if self.returncode is not None:
            return self.returncode
        return await self.proc.wait()

    async def communicate(self, input_data: bytes | None = None) -> tuple[bytes, bytes]:
        """Write bytes to process and read back results."""
        if self.closed:
            return (b"", b"")
        async with self._stdout_lock, self._stdin_lock, self._stderr_lock:
            stdout, stderr = await self.proc.communicate(input_data)
        return (stdout, stderr)

    async def read_stderr(self) -> AsyncGenerator[bytes, None]:
        """Read lines from the stderr stream."""
        async with self._stderr_lock:
            async for line in self.proc.stderr:
                if self.closed:
                    break
                yield line


async def check_output(shell_cmd: str) -> tuple[int, bytes]:
    """Run shell subprocess and return output."""
    proc = await asyncio.create_subprocess_shell(
        shell_cmd,
        stderr=asyncio.subprocess.STDOUT,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return (proc.returncode, stdout)
