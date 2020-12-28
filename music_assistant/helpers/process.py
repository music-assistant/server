"""
Implementation of a (truly) non blocking subprocess.

The subprocess implementation in asyncio can (still) sometimes cause deadlocks,
even when properly handling reading/writes from different tasks.
Besides that, when using multiple asyncio subprocesses, together with uvloop
things go very wrong: https://github.com/MagicStack/uvloop/issues/317

As we rely a lot on moving chunks around through subprocesses (mainly sox),
this custom implementation can be seen as a temporary solution until the main issue
in uvloop is resolved.
"""

import asyncio
import logging
import subprocess
from typing import AsyncGenerator, List, Optional

LOGGER = logging.getLogger("AsyncProcess")

DEFAULT_CHUNKSIZE = 512000


class AsyncProcess:
    """Implementation of a (truly) non blocking subprocess."""

    # workaround that is compatible with uvloop

    def __init__(
        self, process_args: List, enable_write: bool = False, enable_shell=False
    ):
        """Initialize."""
        self._id = "".join(process_args)
        self._proc = subprocess.Popen(
            process_args,
            shell=enable_shell,
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE if enable_write else None,
            # bufsize needs to be very high for smooth playback
            bufsize=4000000,
        )
        self.loop = asyncio.get_running_loop()
        self._cancelled = False

    async def __aenter__(self) -> "AsyncProcess":
        """Enter context manager."""
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self._cancelled = True
        if self._proc.poll() is None:
            # process needs to be cleaned up..
            await self.loop.run_in_executor(None, self.__close)

        return exc_type

    async def iterate_chunks(
        self, chunksize: int = DEFAULT_CHUNKSIZE
    ) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the process stdout. Generator."""
        while True:
            chunk = await self.read(chunksize)
            yield chunk
            if len(chunk) < chunksize:
                # last chunk
                break

    async def read(self, chunksize: int = DEFAULT_CHUNKSIZE) -> bytes:
        """Read x bytes from the process stdout."""
        if self._cancelled:
            raise asyncio.CancelledError()
        return await self.loop.run_in_executor(None, self.__read, chunksize)

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self._cancelled:
            raise asyncio.CancelledError()
        await self.loop.run_in_executor(None, self.__write, data)

    async def write_eof(self) -> None:
        """Write eof to process."""
        if self._cancelled:
            raise asyncio.CancelledError()
        await self.loop.run_in_executor(None, self.__write_eof)

    async def communicate(self, input_data: Optional[bytes] = None) -> bytes:
        """Write bytes to process and read back results."""
        if self._cancelled:
            raise asyncio.CancelledError()
        return await self.loop.run_in_executor(None, self._proc.communicate, input_data)

    def __read(self, chunksize: int = DEFAULT_CHUNKSIZE):
        """Try read chunk from process."""
        try:
            return self._proc.stdout.read(chunksize)
        except (BrokenPipeError, ValueError, AttributeError):
            # Process already exited
            return b""

    def __write(self, data: bytes):
        """Write data to process stdin."""
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, AttributeError):
            # Process already exited
            pass

    def __write_eof(self):
        """Write eof to process stdin."""
        try:
            self._proc.stdin.close()
        except (BrokenPipeError, ValueError, AttributeError):
            # Process already exited
            pass

    def __close(self):
        """Prevent subprocess deadlocking, make sure it closes."""
        LOGGER.debug("Cleaning up process %s...", self._id)
        # close stdout
        if not self._proc.stdout.closed:
            try:
                self._proc.stdout.close()
            except BrokenPipeError:
                pass
        # close stdin if needed
        if self._proc.stdin and not self._proc.stdin.closed:
            try:
                self._proc.stdin.close()
            except BrokenPipeError:
                pass
        # send terminate
        self._proc.terminate()
        # wait for exit
        try:
            self._proc.wait(5)
            LOGGER.debug("Process %s exited with %s.", self._id, self._proc.returncode)
        except subprocess.TimeoutExpired:
            LOGGER.error("Process %s did not terminate in time.", self._id)
            self._proc.kill()
        LOGGER.debug("Process %s closed.", self._id)


class AsyncProcessBroken:
    """Implementation of a (truly) non blocking subprocess."""

    # this version is not compatible with uvloop

    def __init__(self, process_args: List, enable_write: bool = False):
        """Initialize."""
        self._proc = None
        self._process_args = process_args
        self._enable_write = enable_write
        self._cancelled = False

    async def __aenter__(self) -> "AsyncProcess":
        """Enter context manager."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._process_args,
            stdin=asyncio.subprocess.PIPE if self._enable_write else None,
            stdout=asyncio.subprocess.PIPE,
            limit=64000000
        )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self._cancelled = True
        LOGGER.debug("subprocess exit requested")
        if self._proc.returncode is None:
            # prevent subprocess deadlocking, send terminate and read remaining bytes
            if self._enable_write and self._proc.stdin.can_write_eof():
                self._proc.stdin.write_eof()
            self._proc.terminate()
            await self._proc.stdout.read()
        del self._proc
        LOGGER.debug("subprocess exited")

    async def iterate_chunks(
        self, chunk_size: int = DEFAULT_CHUNKSIZE
    ) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the process stdout. Generator."""
        while True:
            chunk = await self.read(chunk_size)
            yield chunk
            if len(chunk) < chunk_size:
                break

    async def read(self, chunk_size: int = DEFAULT_CHUNKSIZE) -> bytes:
        """Read x bytes from the process stdout."""
        if self._cancelled:
            raise asyncio.CancelledError()
        try:
            return await self._proc.stdout.readexactly(chunk_size)
        except asyncio.IncompleteReadError as err:
            return err.partial

    async def write(self, data: bytes) -> None:
        """Write data to process stdin."""
        if self._cancelled:
            raise asyncio.CancelledError()
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    async def write_eof(self) -> None:
        """Write eof to process."""
        if self._cancelled:
            raise asyncio.CancelledError()
        if self._proc.stdin.can_write_eof():
            self._proc.stdin.write_eof()

    async def communicate(self, input_data: Optional[bytes] = None) -> bytes:
        """Write bytes to process and read back results."""
        if self._cancelled:
            raise asyncio.CancelledError()
        return await self._proc.communicate(input_data)
