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

# if TYPE_CHECKING:
from collections.abc import AsyncGenerator
from contextlib import suppress
from signal import SIGINT
from types import TracebackType
from typing import TYPE_CHECKING

from music_assistant.constants import MASS_LOGGER_NAME

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.process")

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
        stdin: bool | int | AsyncGenerator[bytes, None] | None = None,
        stdout: bool | int | None = None,
        stderr: bool | int | None = False,
        name: str | None = None,
    ) -> None:
        """Initialize AsyncProcess."""
        self.proc: asyncio.subprocess.Process | None = None
        if name is None:
            name = args[0].split(os.sep)[-1]
        self.name = name
        self.attached_tasks: list[asyncio.Task] = []
        self.logger = LOGGER.getChild(name)
        self._args = args
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._stdin_enabled = stdin not in (None, False)
        self._stdout_enabled = stdout not in (None, False)
        self._stderr_enabled = stderr not in (None, False)
        self._close_called = False
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
        # send interrupt signal to process when we're cancelled
        await self.close(send_signal=exc_type in (GeneratorExit, asyncio.CancelledError))
        self._returncode = self.returncode

    async def start(self) -> None:
        """Perform Async init of process."""
        self.proc = await asyncio.create_subprocess_exec(
            *self._args,
            stdin=asyncio.subprocess.PIPE
            if (self._stdin is True or isinstance(self._stdin, AsyncGenerator))
            else self._stdin,
            stdout=asyncio.subprocess.PIPE if self._stdout is True else self._stdout,
            stderr=asyncio.subprocess.PIPE if self._stderr is True else self._stderr,
        )
        self.logger.debug("Process %s started with PID %s", self.name, self.proc.pid)
        if isinstance(self._stdin, AsyncGenerator):
            self.attached_tasks.append(asyncio.create_task(self._feed_stdin()))

    async def iter_chunked(self, n: int = DEFAULT_CHUNKSIZE) -> AsyncGenerator[bytes, None]:
        """Yield chunks of n size from the process stdout."""
        while not self._close_called:
            chunk = await self.readexactly(n)
            if chunk == b"":
                break
            yield chunk

    async def iter_any(self, n: int = DEFAULT_CHUNKSIZE) -> AsyncGenerator[bytes, None]:
        """Yield chunks as they come in from process stdout."""
        while not self._close_called:
            chunk = await self.read(n)
            if chunk == b"":
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
        if self._close_called:
            raise RuntimeError("write called while process already done")
        self.proc.stdin.write(data)
        with suppress(BrokenPipeError, ConnectionResetError):
            await self.proc.stdin.drain()

    async def write_eof(self) -> None:
        """Write end of file to to process stdin."""
        if self._close_called:
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

    async def close(self, send_signal: bool = False) -> int:
        """Close/terminate the process and wait for exit."""
        self._close_called = True
        # close any/all attached (writer) tasks
        for task in self.attached_tasks:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if send_signal and self.returncode is None:
            self.proc.send_signal(SIGINT)
            # allow the process a bit of time to respond to the signal before we go nuclear
            await asyncio.sleep(0.5)

        # make sure the process is really cleaned up.
        # especially with pipes this can cause deadlocks if not properly guarded
        # we need to ensure stdout and stderr are flushed and stdin closed
        while True:
            try:
                async with asyncio.timeout(5):
                    # abort existing readers on stderr/stdout first before we send communicate
                    if self.proc.stdout and self.proc.stdout._waiter is not None:
                        self.proc.stdout._waiter.set_exception(asyncio.CancelledError())
                        self.proc.stdout._waiter = None
                    if self.proc.stderr and self.proc.stderr._waiter is not None:
                        self.proc.stderr._waiter.set_exception(asyncio.CancelledError())
                        self.proc.stderr._waiter = None
                    # use communicate to flush all pipe buffers
                    await self.proc.communicate()
                    if self.returncode is not None:
                        break
            except TimeoutError:
                self.logger.debug(
                    "Process %s with PID %s did not stop in time. Sending terminate...",
                    self.name,
                    self.proc.pid,
                )
                self.proc.terminate()
        self.logger.debug(
            "Process %s with PID %s stopped with returncode %s",
            self.name,
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

    async def _feed_stdin(self) -> None:
        """Feed stdin with chunks from an AsyncGenerator."""
        if TYPE_CHECKING:
            self._stdin: AsyncGenerator[bytes, None]
        try:
            async for chunk in self._stdin:
                if self._close_called or self.proc.stdin.is_closing():
                    return
                await self.write(chunk)
            await self.write_eof()
        except Exception as err:
            if not isinstance(err, asyncio.CancelledError):
                self.logger.exception(err)
            # make sure the stdin generator is also properly closed
            # by propagating a cancellederror within
            task = asyncio.create_task(self._stdin.__anext__())
            task.cancel()

    async def read_stderr(self) -> bytes:
        """Read line from stderr."""
        try:
            return await self.proc.stderr.readline()
        except ValueError as err:
            # we're waiting for a line (separator found), but the line was too big
            # this may happen with ffmpeg during a long (radio) stream where progress
            # gets outputted to the stderr but no newline
            # https://stackoverflow.com/questions/55457370/how-to-avoid-valueerror-separator-is-not-found-and-chunk-exceed-the-limit
            # NOTE: this consumes the line that was too big
            if "chunk exceed the limit" in str(err):
                return await self.proc.stderr.readline()
            # raise for all other (value) errors
            raise

    async def iter_stderr(self) -> AsyncGenerator[str, None]:
        """Iterate lines from the stderr stream as string."""
        while True:
            line = await self.read_stderr()
            if line == b"":
                break
            line = line.decode().strip()
            if not line:
                continue
            yield line


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
