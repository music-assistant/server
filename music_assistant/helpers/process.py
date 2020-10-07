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

LOGGER = logging.getLogger("AsyncProcess")


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
        self._exit = False
        self._proc = None
        self._id = int(time.time())  # some identifier for logging

    async def __aenter__(self) -> "AsyncProcess":
        """Enter context manager, start running the process in executor."""
        LOGGER.debug("[%s] Entered context manager", self._id)
        self._proc = subprocess.Popen(
            self._process_args,
            **{
                "shell": self._enable_shell,
                "stdout": subprocess.PIPE,
                "stdin": subprocess.PIPE if self._enable_write else None,
            },
        )
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
        else:
            LOGGER.debug("[%s] Context manager exit", self._id)
        # signal that we must exit
        self._exit = True

        def close_proc():
            if self._proc and self._proc.poll() is None:
                # there is no real clean way to do this with all the blocking pipes
                self._proc.kill()

        await asyncio.get_running_loop().run_in_executor(None, close_proc)
        LOGGER.debug("[%s] Cleanup finished", self._id)
        return True

    async def iterate_chunks(self) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the output Queue. Generator."""
        LOGGER.debug("[%s] start reading from generator", self._id)
        while not self._exit:
            chunk = await self.read()
            yield chunk
            if len(chunk) < self._chunksize:
                break
        LOGGER.debug("[%s] finished reading from generator", self._id)

    async def read(self) -> bytes:
        """Read single chunk from the output Queue."""

        def try_read():
            try:
                data = self._proc.stdout.read(self._chunksize)
                return data
            except BrokenPipeError:
                return b""
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.exception(exc)
                return b""

        return await asyncio.get_running_loop().run_in_executor(None, try_read)

    async def write(self, data: bytes) -> None:
        """Write data to process."""

        def try_write(_data):
            try:
                self._proc.stdin.write(_data)
            except BrokenPipeError:
                pass
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.exception(exc)

        await asyncio.get_running_loop().run_in_executor(None, try_write, data)

    async def write_eof(self) -> None:
        """Write eof to process."""

        def try_write():
            try:
                self._proc.stdin.close()
            except BrokenPipeError:
                pass
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.exception(exc)

        await asyncio.get_running_loop().run_in_executor(None, try_write)

    async def communicate(self, input_data: Optional[bytes] = None) -> bytes:
        """Write bytes to process and read back results."""
        if not self._enable_write and input_data:
            raise RuntimeError("Write is disabled")
        if input_data:
            await self.write(input_data)
        output = b""
        async for chunk in self.iterate_chunks():
            output += chunk
        return output


# first attempt with queues, too complicated
# left here as reference
class AsyncProcessWithQueues(object):
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
        # we have large chunks, limit the queue size a bit.
        import janus

        self.__queue_in = janus.Queue(8)
        self.__queue_out = janus.Queue(4)
        self.__proc_task = None
        self._exit = threading.Event()
        self._id = int(time.time())  # some identifier for logging

    async def __aenter__(self) -> "AsyncProcess":
        """Enter context manager, start running the process in executor."""
        LOGGER.debug("[%s] Entered context manager", self._id)
        self.__proc_task = asyncio.get_running_loop().run_in_executor(None, self._run)
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
        else:
            LOGGER.debug("[%s] Context manager exit", self._id)
        # signal that we must exit
        self._exit.set()
        # if self._proc and self._proc.poll() is None:
        #     asyncio.get_running_loop().run_in_executor(None, self._proc.communicate)
        print("1")
        self.__queue_out.close()
        self.__queue_in.close()
        print("2")
        await self.__queue_out.wait_closed()
        await self.__queue_in.wait_closed()
        print("3")
        # await executor job
        self.__proc_task.cancel()
        # await self.__proc_task
        print("4")

        LOGGER.debug("[%s] Cleanup finished", self._id)
        return True

    async def iterate_chunks(self) -> AsyncGenerator[bytes, None]:
        """Yield chunks from the output Queue. Generator."""
        LOGGER.debug("[%s] start reading from generator", self._id)
        while not self._exit.is_set():
            chunk = await self.__queue_out.async_q.get()
            self.__queue_out.async_q.task_done()
            if not chunk:
                break
            yield chunk
        LOGGER.debug("[%s] finished reading from generator", self._id)

    async def read(self) -> bytes:
        """Read single chunk from the output Queue."""
        chunk = await self.__queue_out.async_q.get()
        self.__queue_out.async_q.task_done()
        return chunk

    async def write(self, data: Optional[bytes] = None) -> None:
        """Write data to process."""
        if not self._exit.is_set():
            await self.__queue_in.async_q.put(data)

    async def write_eof(self) -> None:
        """Write eof to process stdin."""
        await self.write(b"")

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

    def _run(self):
        """Run actual process in executor thread."""
        LOGGER.info(
            "[%s] Starting process with args: %s", self._id, str(self._process_args)
        )
        proc = subprocess.Popen(
            self._process_args,
            **{
                "shell": self._enable_shell,
                "stdout": subprocess.PIPE,
                "stdin": subprocess.PIPE if self._enable_write else None,
            },
        )

        # start fill buffer task in (yet another) background thread
        def fill_buffer():
            LOGGER.debug("[%s] start fill buffer", self._id)
            try:
                while not self._exit.is_set() and not self.__queue_in.closed:
                    chunk = self.__queue_in.sync_q.get()
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
            except Exception as exc:  # pylint: disable=broad-except
                LOGGER.debug("[%s], fill buffer aborted (%s)", self._id, str(exc))
            else:
                LOGGER.debug("[%s] fill buffer finished", self._id)

        if self._enable_write:
            fill_buffer_thread = threading.Thread(
                target=fill_buffer, name=f"AsyncProcess_{self._id}"
            )
            fill_buffer_thread.start()

        # consume bytes from stdout
        try:
            while not self._exit.is_set() and not self.__queue_out.closed:
                chunk = proc.stdout.read(self._chunksize)
                self.__queue_out.sync_q.put(chunk)
                if len(chunk) < self._chunksize:
                    LOGGER.debug("[%s] last chunk received on stdout", self._id)
                    break
            if self._enable_write:
                fill_buffer_thread.join()
            # write empty chunk to out queue to indicate end of stream just in case
            self.__queue_out.sync_q.put(b"")
        finally:
            LOGGER.info("[%s] wait for process exit", self._id)
            # pickup remaining bytes if process is stull running
            if proc.poll() is None:
                proc.communicate()
