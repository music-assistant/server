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
from typing import Self

from music_assistant.constants import MASS_LOGGER_NAME, VERBOSE_LOG_LEVEL

LOGGER = logging.getLogger(f"{MASS_LOGGER_NAME}.helpers.process")

DEFAULT_CHUNKSIZE = 64000

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
        stdin: bool | int | None = None,
        stdout: bool | int | None = None,
        stderr: bool | int | None = False,
        name: str | None = None,
    ) -> None:
        """Initialize AsyncProcess."""
        self.proc: asyncio.subprocess.Process | None = None
        if name is None:
            name = args[0].split(os.sep)[-1]
        self.name = name
        self.logger = LOGGER.getChild(name)
        self._args = args
        self._stdin = None if stdin is False else stdin
        self._stdout = None if stdout is False else stdout
        self._stderr = asyncio.subprocess.DEVNULL if stderr is False else stderr
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

    async def __aenter__(self) -> Self:
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
        for attempt in range(2):
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self._args,
                    stdin=asyncio.subprocess.PIPE if self._stdin is True else self._stdin,
                    stdout=asyncio.subprocess.PIPE if self._stdout is True else self._stdout,
                    stderr=asyncio.subprocess.PIPE if self._stderr is True else self._stderr,
                    # because we're exchanging big amounts of (audio) data with pipes
                    # it makes sense to extend the pipe size and (buffer) limits a bit
                    limit=1000000 if attempt == 0 else 65536,
                    pipesize=1000000 if attempt == 0 else -1,
                )
                break
            except PermissionError:
                if attempt > 0:
                    raise
                LOGGER.error(
                    "Detected that you are running the (docker) container without "
                    "permissive access rights. This will impact performance !"
                )

        self.logger.log(
            VERBOSE_LOG_LEVEL, "Process %s started with PID %s", self.name, self.proc.pid
        )

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
        if self._close_called:
            return b""
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
        if self._close_called:
            return b""
        return await self.proc.stdout.read(n)

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self.closed:
            self.logger.warning("write called while process already done")
            return
        self.proc.stdin.write(data)
        with suppress(BrokenPipeError, ConnectionResetError):
            await self.proc.stdin.drain()

    async def write_eof(self) -> None:
        """Write end of file to to process stdin."""
        if self.closed:
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

    async def read_stderr(self) -> bytes:
        """Read line from stderr."""
        if self._close_called:
            return b""
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

    async def close(self, send_signal: bool = False) -> None:
        """Close/terminate the process and wait for exit."""
        self._close_called = True
        if send_signal and self.returncode is None:
            self.proc.send_signal(SIGINT)
        if self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        # abort existing readers on stderr/stdout first before we send communicate
        waiter: asyncio.Future
        if self.proc.stdout and (waiter := self.proc.stdout._waiter):
            self.proc.stdout._waiter = None
            if waiter and not waiter.done():
                waiter.set_exception(asyncio.CancelledError())
        if self.proc.stderr and (waiter := self.proc.stderr._waiter):
            self.proc.stderr._waiter = None
            if waiter and not waiter.done():
                waiter.set_exception(asyncio.CancelledError())
        await asyncio.sleep(0)  # yield to loop

        # make sure the process is really cleaned up.
        # especially with pipes this can cause deadlocks if not properly guarded
        # we need to ensure stdout and stderr are flushed and stdin closed
        while self.returncode is None:
            try:
                # use communicate to flush all pipe buffers
                await asyncio.wait_for(self.proc.communicate(), 5)
            except RuntimeError as err:
                if "read() called while another coroutine" in str(err):
                    # race condition
                    continue
                raise
            except TimeoutError:
                self.logger.debug(
                    "Process %s with PID %s did not stop in time. Sending terminate...",
                    self.name,
                    self.proc.pid,
                )
                self.proc.terminate()
        self.logger.log(
            VERBOSE_LOG_LEVEL,
            "Process %s with PID %s stopped with returncode %s",
            self.name,
            self.proc.pid,
            self.returncode,
        )

    async def wait(self) -> int:
        """Wait for the process and return the returncode."""
        if self._returncode is None:
            self._returncode = await self.proc.wait()
        return self._returncode


async def check_output(*args: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Run subprocess and return returncode and output."""
    proc = await asyncio.create_subprocess_exec(
        *args, stderr=asyncio.subprocess.STDOUT, stdout=asyncio.subprocess.PIPE, env=env
    )
    stdout, _ = await proc.communicate()
    return (proc.returncode, stdout)


async def communicate(
    args: list[str],
    input: bytes | None = None,  # noqa: A002
) -> tuple[int, bytes, bytes]:
    """Communicate with subprocess and return returncode, stdout and stderr output."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stderr=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if input is not None else None,
    )
    stdout, stderr = await proc.communicate(input)
    return (proc.returncode, stdout, stderr)
