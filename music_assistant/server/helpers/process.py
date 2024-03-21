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
        args: list[str] | str,
        enable_stdin: bool = False,
        enable_stdout: bool = True,
        enable_stderr: bool = False,
        custom_stdin: AsyncGenerator[bytes, None] | int | None = None,
        custom_stdout: int | None = None,
        name: str | None = None,
    ) -> None:
        """Initialize AsyncProcess."""
        self.proc: asyncio.subprocess.Process | None = None
        self._args = args
        self._enable_stdin = enable_stdin
        self._enable_stdout = enable_stdout
        self._enable_stderr = enable_stderr
        self._close_called = False
        self._returncode: bool | None = None
        self._custom_stdout = custom_stdout
        self._name = name
        self.attached_tasks: list[asyncio.Task] = []
        if isinstance(custom_stdin, int):
            self._custom_stdin = custom_stdin
        elif custom_stdin:
            self.attached_tasks.append(asyncio.create_task(self._feed_stdin(custom_stdin)))
        else:
            self._custom_stdin = None
        self._custom_stdout = custom_stdout

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

    async def start(self) -> None:
        """Perform Async init of process."""
        stdin = self._custom_stdin if self._custom_stdin is not None else asyncio.subprocess.PIPE
        stdout = self._custom_stdout if self._custom_stdout is not None else asyncio.subprocess.PIPE
        if isinstance(self._args, str):
            proc_name_simple = self._args.split(" ")[0].split(os.sep)[-1]
            self.proc = await asyncio.create_subprocess_shell(
                self._args,
                stdin=stdin if self._enable_stdin else None,
                stdout=stdout if self._enable_stdout else None,
                stderr=asyncio.subprocess.PIPE if self._enable_stderr else None,
            )
        else:
            self.proc = await asyncio.create_subprocess_exec(
                *self._args,
                stdin=stdin if self._enable_stdin else None,
                stdout=stdout if self._enable_stdout else None,
                stderr=asyncio.subprocess.PIPE if self._enable_stderr else None,
            )
            proc_name_simple = self._args[0].split(os.sep)[-1]
        self._name = self._name or proc_name_simple
        LOGGER.debug("Started %s with PID %s", self._name, self.proc.pid)

    async def iter_chunked(self, n: int = DEFAULT_CHUNKSIZE) -> AsyncGenerator[bytes, None]:
        """Yield chunks of n size from the process stdout."""
        while True:
            chunk = await self.readexactly(n)
            if len(chunk) == 0:
                break
            yield chunk

    async def iter_any(self, n: int = DEFAULT_CHUNKSIZE) -> AsyncGenerator[bytes, None]:
        """Yield chunks as they come in from process stdout."""
        while True:
            chunk = await self.read(n)
            if len(chunk) == 0:
                break
            yield chunk

    async def readexactly(self, n: int) -> bytes:
        """Read exactly n bytes from the process stdout (or less if eof)."""
        try:
            return await self.proc.stdout.readexactly(n)
        except asyncio.IncompleteReadError as err:
            return err.partial

    async def read(self, n: int) -> bytes:
        """Read up to n bytes from the stdout stream.

        If n is positive, this function try to read n bytes,
        and may return less or equal bytes than requested, but at least one byte.
        If EOF was received before any byte is read, this function returns empty byte object.
        """
        return await self.proc.stdout.read(n)

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        self.proc.stdin.write(data)
        with suppress(BrokenPipeError, ConnectionResetError):
            await self.proc.stdin.drain()

    async def write_eof(self) -> None:
        """Write end of file to to process stdin."""
        if self._close_called or self.proc.stdin.is_closing():
            return
        try:
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
        for task in self.attached_tasks:
            if not task.done():
                with suppress(asyncio.CancelledError):
                    task.cancel()
                    await task
        while self.proc.returncode is None:
            # make sure the process is cleaned up
            try:
                # we need to use communicate to ensure buffers are flushed
                await asyncio.wait_for(self.proc.communicate(), 0.5)
            except TimeoutError:
                LOGGER.debug(
                    "Process %s with PID %s did not stop in time. Sending terminate...",
                    self._name,
                    self.proc.pid,
                )
                self.proc.terminate()
        LOGGER.debug(
            "Process %s with PID %s stopped with returncode %s",
            self._name,
            self.proc.pid,
            self.proc.returncode,
        )
        return self.proc.returncode

    async def wait(self) -> int:
        """Wait for the process and return the returncode."""
        if self.returncode is not None:
            return self.returncode
        return await self.proc.wait()

    async def communicate(self, input_data: bytes | None = None) -> tuple[bytes, bytes]:
        """Write bytes to process and read back results."""
        stdout, stderr = await self.proc.communicate(input_data)
        return (stdout, stderr)

    async def read_stderr(self) -> AsyncGenerator[bytes, None]:
        """Read lines from the stderr stream."""
        async for line in self.proc.stderr:
            yield line

    async def _feed_stdin(self, custom_stdin: AsyncGenerator[bytes, None]) -> None:
        """Feed stdin with chunks from an AsyncGenerator."""
        async for chunk in custom_stdin:
            await self.write(chunk)
        await self.write_eof()


async def check_output(shell_cmd: str) -> tuple[int, bytes]:
    """Run shell subprocess and return output."""
    proc = await asyncio.create_subprocess_shell(
        shell_cmd,
        stderr=asyncio.subprocess.STDOUT,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return (proc.returncode, stdout)
