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
import threading
import time
from typing import AsyncGenerator, List, Optional

LOGGER = logging.getLogger("mass.helpers")


class AsyncProcess(object):
    """Implementation of a (truly) non blocking subprocess."""

    def __init__(
        self,
        process_args: List,
        chunksize=512000,
        enable_write: bool = False,
        enable_shell=False,
    ):
        """Initialize."""
        self._process_args = process_args
        self._chunksize = chunksize
        self._enable_write = enable_write
        self._enable_shell = enable_shell
        self.loop = asyncio.get_running_loop()
        self.__queue_in = asyncio.Queue(4)
        self.__queue_out = asyncio.Queue(8)
        self.__proc_task = None
        self._exit = False
        self._id = int(time.time())  # some identifier for logging

    async def __aenter__(self) -> "AsyncProcess":
        """Enter context manager, start running the process in executor."""
        self.__proc_task = self.loop.run_in_executor(None, self.__run_proc)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        if exc_type:
            LOGGER.debug(
                "[%s] Context manager exit with exception %s (%s)",
                self._id,
                exc_type,
                str(exc_value),
            )

        self._exit = True
        # prevent a deadlock by clearing the queues
        while self.__queue_in.qsize():
            await self.__queue_in.get()
            self.__queue_in.task_done()
        self.__queue_in.put_nowait(b"")
        while self.__queue_out.qsize():
            await self.__queue_out.get()
            self.__queue_out.task_done()
        await self.__proc_task
        return True

    async def iterate_chunks(self) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the output Queue. Generator."""
        while True:
            chunk = await self.read()
            yield chunk
            if not chunk or len(chunk) < self._chunksize:
                break

    async def read(self) -> bytes:
        """Read single chunk from the output Queue."""
        if self._exit:
            raise RuntimeError("Already exited")
        data = await self.__queue_out.get()
        self.__queue_out.task_done()
        return data

    async def write(self, data: bytes) -> None:
        """Write data to process."""
        if self._exit:
            raise RuntimeError("Already exited")
        await self.__queue_in.put(data)

    async def write_eof(self) -> None:
        """Write eof to process."""
        await self.__queue_in.put(b"")

    async def communicate(self, input_data: Optional[bytes] = None) -> bytes:
        """Write bytes to process and read back results."""
        if not self._enable_write and input_data:
            raise RuntimeError("Write is disabled")
        if input_data:
            await self.write(input_data)
            await self.write_eof()
        output = b""
        async for chunk in self.iterate_chunks():
            output += chunk
        return output

    def __run_proc(self):
        """Run process in executor."""
        try:
            proc = subprocess.Popen(
                self._process_args,
                shell=self._enable_shell,
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE if self._enable_write else None,
            )
            if self._enable_write:
                threading.Thread(
                    target=self.__write_stdin,
                    args=(proc.stdin,),
                    name=f"AsyncProcess_{self._id}_write_stdin",
                    daemon=True,
                ).start()
            threading.Thread(
                target=self.__read_stdout,
                args=(proc.stdout,),
                name=f"AsyncProcess_{self._id}_read_stdout",
                daemon=True,
            ).start()
            proc.wait()

        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.warning("[%s] process exiting abormally: %s", self._id, str(exc))
            LOGGER.exception(exc)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate()

    def __write_stdin(self, _stdin):
        """Put chunks from queue to stdin."""
        try:
            while True:
                chunk = asyncio.run_coroutine_threadsafe(
                    self.__queue_in.get(), self.loop
                ).result()
                self.__queue_in.task_done()
                if not chunk:
                    _stdin.close()
                    break
                _stdin.write(chunk)
        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.debug(
                "[%s] write to stdin aborted with exception: %s", self._id, str(exc)
            )

    def __read_stdout(self, _stdout):
        """Put chunks from stdout to queue."""
        try:
            while True:
                chunk = _stdout.read(self._chunksize)
                asyncio.run_coroutine_threadsafe(
                    self.__queue_out.put(chunk), self.loop
                ).result()
                if not chunk or len(chunk) < self._chunksize:
                    break
            # write empty chunk just in case
            asyncio.run_coroutine_threadsafe(self.__queue_out.put(b""), self.loop)
        except Exception as exc:  # pylint: disable=broad-except
            LOGGER.debug(
                "[%s] read from stdout aborted with exception: %s", self._id, str(exc)
            )
