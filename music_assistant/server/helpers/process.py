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
from signal import SIGINT
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
        self._name = name or self._args[0].split(os.sep)[-1]
        self.attached_tasks: list[asyncio.Task] = []
        self._custom_stdin = custom_stdin
        if not isinstance(custom_stdin, int | None):
            self._custom_stdin = None
            self.attached_tasks.append(asyncio.create_task(self._feed_stdin(custom_stdin)))
        self._custom_stdout = custom_stdout
        self._stderr_lock = asyncio.Lock()
        self._stdout_lock = asyncio.Lock()

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
        if (ret_code := self.proc.returncode) is not None:
            self._returncode = ret_code
        return ret_code

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
        self.proc = await asyncio.create_subprocess_exec(
            *self._args,
            stdin=stdin if self._enable_stdin else None,
            stdout=stdout if self._enable_stdout else None,
            stderr=asyncio.subprocess.PIPE if self._enable_stderr else None,
        )
        LOGGER.debug("Started %s with PID %s", self._name, self.proc.pid)

    async def iter_chunked(self, n: int = DEFAULT_CHUNKSIZE) -> AsyncGenerator[bytes, None]:
        """Yield chunks of n size from the process stdout."""
        while not self._close_called:
            chunk = await self.readexactly(n)
            if len(chunk) == 0:
                break
            yield chunk

    async def iter_any(self, n: int = DEFAULT_CHUNKSIZE) -> AsyncGenerator[bytes, None]:
        """Yield chunks as they come in from process stdout."""
        while not self._close_called:
            chunk = await self.read(n)
            if len(chunk) == 0:
                break
            yield chunk

    async def readexactly(self, n: int) -> bytes:
        """Read exactly n bytes from the process stdout (or less if eof)."""
        if not self.proc.stdout or self.proc.stdout.at_eof():
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
        if not self.proc.stdout or self.proc.stdout.at_eof():
            return b""
        async with self._stdout_lock:
            return await self.proc.stdout.read(n)

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self._close_called or self.proc.stdin.is_closing():
            raise asyncio.CancelledError("write called while process already done")
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
        # close any/all attached (writer) tasks
        for task in self.attached_tasks:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        # make sure the process is really cleaned up.
        # especially with pipes this can cause deadlocks if not properly guarded
        # we need to ensure stdout and stderr are flushed and stdin closed
        while self.returncode is None:
            if not self.proc.stdin:
                self.proc.send_signal(SIGINT)
            try:
                async with asyncio.timeout(30):
                    # wait for stdout/stderr locks if needed
                    await self._stdout_lock.acquire()
                    await self._stderr_lock.acquire()
                    # use communicate to flush all pipe buffers
                    await self.proc.communicate()
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
            self.returncode,
        )
        return self.returncode

    async def wait(self) -> int:
        """Wait for the process and return the returncode."""
        if self.returncode is not None:
            return self.returncode
        self._returncode = await self.proc.wait()
        return self._returncode

    async def communicate(self, input_data: bytes | None = None) -> tuple[bytes, bytes]:
        """Write bytes to process and read back results."""
        stdout, stderr = await self.proc.communicate(input_data)
        self._returncode = self.proc.returncode
        return (stdout, stderr)

    async def iter_stderr(self) -> AsyncGenerator[bytes, None]:
        """Iterate lines from the stderr stream."""
        while not self.closed:
            if self.proc.stderr.at_eof():
                break
            try:
                async with self._stderr_lock:
                    yield await self.proc.stderr.readline()
            except ValueError as err:
                # we're waiting for a line (separator found), but the line was too big
                # this may happen with ffmpeg during a long (radio) stream where progress
                # gets outputted to the stderr but no newline
                # https://stackoverflow.com/questions/55457370/how-to-avoid-valueerror-separator-is-not-found-and-chunk-exceed-the-limit
                # NOTE: this consumes the line that was too big
                if "chunk exceed the limit" in str(err):
                    continue
                # raise for all other (value) errors
                raise

    async def _feed_stdin(self, custom_stdin: AsyncGenerator[bytes, None]) -> None:
        """Feed stdin with chunks from an AsyncGenerator."""
        try:
            async for chunk in custom_stdin:
                if self._close_called or self.proc.stdin.is_closing():
                    return
                await self.write(chunk)
            await self.write_eof()
        except asyncio.CancelledError:
            # make sure the stdin generator is also properly closed
            # by propagating a cancellederror within
            task = asyncio.create_task(custom_stdin.__anext__())
            task.cancel()


async def check_output(args: str | list[str]) -> tuple[int, bytes]:
    """Run subprocess and return output."""
    if isinstance(args, str):
        proc = await asyncio.create_subprocess_shell(
            args,
            stderr=asyncio.subprocess.STDOUT,
            stdout=asyncio.subprocess.PIPE,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stderr=asyncio.subprocess.STDOUT,
            stdout=asyncio.subprocess.PIPE,
        )
    stdout, _ = await proc.communicate()
    return (proc.returncode, stdout)
