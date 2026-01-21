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


def get_subprocess_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Get environment for subprocess, stripping LD_PRELOAD to avoid jemalloc warnings."""
    result = dict(os.environ)
    result.pop("LD_PRELOAD", None)
    if env:
        result.update(env)
    return result


class AsyncProcess:
    """
    AsyncProcess.

    Wrapper around asyncio subprocess to help with using pipe streams and
    taking care of properly closing the process in case of exit (on both success and failures),
    without deadlocking.
    """

    _stdin_feeder_task: asyncio.Task[None] | None = None  # used for ffmpeg
    _stderr_reader_task: asyncio.Task[None] | None = None  # used for ffmpeg

    def __init__(
        self,
        args: list[str],
        stdin: bool | int | None = None,
        stdout: bool | int | None = None,
        stderr: bool | int | None = False,
        name: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Initialize AsyncProcess.

        :param args: Command and arguments to execute.
        :param stdin: Stdin configuration (True for PIPE, False for None, or custom).
        :param stdout: Stdout configuration (True for PIPE, False for None, or custom).
        :param stderr: Stderr configuration (True for PIPE, False for DEVNULL, or custom).
        :param name: Process name for logging.
        :param env: Environment variables for the subprocess (None inherits parent env).
        """
        self.proc: asyncio.subprocess.Process | None = None
        if name is None:
            name = args[0].split(os.sep)[-1]
        self.name = name
        self.logger = LOGGER.getChild(name)
        self._args = args
        self._stdin = None if stdin is False else stdin
        self._stdout = None if stdout is False else stdout
        self._stderr = asyncio.subprocess.DEVNULL if stderr is False else stderr
        self._env = get_subprocess_env(env)
        self._stderr_lock = asyncio.Lock()
        self._stdout_lock = asyncio.Lock()
        self._stdin_lock = asyncio.Lock()
        self._close_called = False
        self._returncode: int | None = None

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
        # make sure we close and cleanup the process
        await self.close()
        self._returncode = self.returncode
        return None

    async def start(self) -> None:
        """Perform Async init of process."""
        self.proc = await asyncio.create_subprocess_exec(
            *self._args,
            stdin=asyncio.subprocess.PIPE if self._stdin is True else self._stdin,
            stdout=asyncio.subprocess.PIPE if self._stdout is True else self._stdout,
            stderr=asyncio.subprocess.PIPE if self._stderr is True else self._stderr,
            env=self._env,
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
        assert self.proc is not None  # for type checking
        assert self.proc.stdout is not None  # for type checking
        async with self._stdout_lock:
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
        assert self.proc is not None  # for type checking
        assert self.proc.stdout is not None  # for type checking
        async with self._stdout_lock:
            return await self.proc.stdout.read(n)

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self._close_called:
            return
        assert self.proc is not None  # for type checking
        assert self.proc.stdin is not None  # for type checking
        async with self._stdin_lock:
            self.proc.stdin.write(data)
            with suppress(BrokenPipeError, ConnectionResetError):
                await self.proc.stdin.drain()

    async def write_eof(self) -> None:
        """Write end of file to to process stdin."""
        if self._close_called:
            return
        assert self.proc is not None  # for type checking
        assert self.proc.stdin is not None  # for type checking
        async with self._stdin_lock:
            try:
                if self.proc.stdin.can_write_eof():
                    self.proc.stdin.write_eof()
                await self.proc.stdin.drain()
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
        if self.returncode is not None:
            return b""
        assert self.proc is not None  # for type checking
        assert self.proc.stderr is not None  # for type checking
        async with self._stderr_lock:
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
        line: str | bytes
        while True:
            line = await self.read_stderr()
            if line == b"":
                break
            line = line.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            yield line

    async def communicate(
        self,
        input: bytes | None = None,  # noqa: A002
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        """Communicate with the process and return stdout and stderr."""
        if self.closed:
            raise RuntimeError("communicate called while process already done")
        # abort existing readers on stderr/stdout first before we send communicate
        await self._stderr_lock.acquire()
        await self._stdout_lock.acquire()
        assert self.proc is not None  # for type checking
        stdout, stderr = await asyncio.wait_for(self.proc.communicate(input), timeout)
        return (stdout, stderr)

    async def close(self) -> None:
        """Close/terminate the process and wait for exit."""
        self._close_called = True
        if not self.proc:
            return

        # cancel existing stdin feeder task if any
        if self._stdin_feeder_task:
            if not self._stdin_feeder_task.done():
                self._stdin_feeder_task.cancel()
            # Always await the task to consume any exception and prevent
            # "Task exception was never retrieved" errors.
            # Suppress CancelledError (from cancel) and any other exception
            # since exceptions have already been propagated through the generator chain.
            with suppress(asyncio.CancelledError, Exception):
                await self._stdin_feeder_task

        # close stdin to signal we're done sending data
        await asyncio.wait_for(self._stdin_lock.acquire(), 10)
        if self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        elif not self.proc.stdin and self.proc.returncode is None:
            self.proc.send_signal(SIGINT)

        # ensure we have no more readers active and stdout is drained
        await asyncio.wait_for(self._stdout_lock.acquire(), 10)
        if self.proc.stdout and not self.proc.stdout.at_eof():
            with suppress(Exception):
                await self.proc.stdout.read(-1)
        # if we have a stderr task active, allow it to finish
        if self._stderr_reader_task:
            await asyncio.wait_for(self._stderr_reader_task, 10)
        elif self.proc.stderr and not self.proc.stderr.at_eof():
            await asyncio.wait_for(self._stderr_lock.acquire(), 10)
            # drain stderr
            with suppress(Exception):
                await self.proc.stderr.read(-1)

        # make sure the process is really cleaned up.
        # especially with pipes this can cause deadlocks if not properly guarded
        # we need to ensure stdout and stderr are flushed and stdin closed
        while self.returncode is None:
            try:
                # use communicate to flush all pipe buffers
                await asyncio.wait_for(self.proc.communicate(), 5)
            except TimeoutError:
                self.logger.debug(
                    "Process %s with PID %s did not stop in time. Sending terminate...",
                    self.name,
                    self.proc.pid,
                )
                with suppress(ProcessLookupError):
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
            assert self.proc is not None
            self._returncode = await self.proc.wait()
        return self._returncode

    async def wait_with_timeout(self, timeout: int) -> int:
        """Wait for the process and return the returncode with a timeout."""
        return await asyncio.wait_for(self.wait(), timeout)

    def attach_stderr_reader(self, task: asyncio.Task[None]) -> None:
        """Attach a stderr reader task to this process."""
        self._stderr_reader_task = task


async def check_output(*args: str, env: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Run subprocess and return returncode and output."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stderr=asyncio.subprocess.STDOUT,
        stdout=asyncio.subprocess.PIPE,
        env=get_subprocess_env(env),
    )
    stdout, _ = await proc.communicate()
    assert proc.returncode is not None  # for type checking
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
        env=get_subprocess_env(),
    )
    stdout, stderr = await proc.communicate(input)
    assert proc.returncode is not None  # for type checking
    return (proc.returncode, stdout, stderr)
