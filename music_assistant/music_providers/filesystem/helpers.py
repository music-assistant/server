"""Some helpers for Filesystem based Musicproviders."""
import asyncio
import os
from io import BytesIO
from queue import Queue
from typing import Any, AsyncGenerator, Dict, NoReturn, Optional

from smb.base import SharedFile, SMBTimeout
from smb.smb_structs import OperationFailure
from smb.SMBConnection import SMBConnection

from music_assistant.helpers.compare import compare_strings
from music_assistant.models.errors import LoginFailed

SERVICE_NAME = "music_assistant"


def get_parentdir(base_path: str, name: str) -> str | None:
    """Look for folder name in path (to find dedicated artist or album folder)."""
    parentdir = os.path.dirname(base_path)
    for _ in range(3):
        dirname = parentdir.rsplit(os.sep)[-1]
        if compare_strings(name, dirname, False):
            return parentdir
        parentdir = os.path.dirname(parentdir)
    return None


def get_relative_path(base_path: str, path: str) -> str:
    """Return the relative path string for a path."""
    if path.startswith(base_path):
        path = path.split(base_path)[1]
    for sep in ("/", "\\"):
        if path.startswith(sep):
            path = path[1:]
    return path


def get_absolute_path(base_path: str, path: str) -> str:
    """Return the absolute path string for a path."""
    if path.startswith(base_path):
        return path
    return os.path.join(base_path, path)


class AsyncSMB:
    """Async wrapped pysmb."""

    def __init__(
        self,
        remote_name: str,
        service_name: str,
        username: str,
        password: str,
        target_ip: str,
        options: Dict[str, Any],
    ) -> None:
        """Initialize instance."""
        self._service_name = service_name
        self._remote_name = remote_name
        self._target_ip = target_ip
        self._username = username
        self._password = password
        self._options = options
        self._conn: SMBConnection = None
        self._cmd_queue = Queue()
        self._runner: asyncio.Future = None

    async def list_path(self, path: str) -> list[SharedFile]:
        """Retrieve a directory listing of files/folders at *path*."""
        return await self._execute(
            "listPath", service_name=self._service_name, path=path
        )

    async def get_attributes(self, path: str) -> SharedFile:
        """Retrieve information about the file at *path* on the *service_name*."""
        return await self._execute(
            "getAttributes", service_name=self._service_name, path=path
        )

    async def retrieve_file(
        self, path: str, offset: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Retrieve file contents."""

        chunk_size = 256000
        while True:
            with BytesIO() as file_obj:
                await self._execute(
                    "retrieveFileFromOffset",
                    service_name=self._service_name,
                    path=path,
                    file_obj=file_obj,
                    offset=offset,
                    max_length=chunk_size,
                )
                file_obj.seek(0)
                chunk = file_obj.read()
                yield chunk
                offset += len(chunk)
                if len(chunk) < chunk_size:
                    break

    async def write_file(self, path: str, data: bytes) -> SharedFile:
        """Store the contents to the file at *path*."""
        with BytesIO() as file_obj:
            file_obj.write(data)
            file_obj.seek(0)
            return await self._execute(
                "storeFile",
                service_name=self._service_name,
                path=path,
                file_obj=file_obj,
            )

    async def path_exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        try:
            await self.get_attributes(file_path)
        except (OperationFailure, SMBTimeout):
            return False
        return True

    async def __aenter__(self) -> "AsyncSMB":
        """Enter context manager."""
        loop = asyncio.get_running_loop()
        # start the smb connection on its own dedicated thread
        self._runner = loop.run_in_executor(None, self._smbconn_runner)
        # send connect command to smb connection
        try:
            assert await self._execute("connect", ip=self._target_ip) is True
        except Exception as exc:
            raise LoginFailed(f"SMB Connect failed to {self._remote_name}") from exc
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self._runner.cancel()

    async def _execute(self, func: str, **kwargs) -> Any:
        """Schedule command/function on the smb connection."""
        fut = asyncio.Future()
        loop = asyncio.get_running_loop()

        def callback(result: Any, err: Optional[BaseException] = None):
            if err:
                loop.call_soon_threadsafe(fut.set_exception, err)
            else:
                loop.call_soon_threadsafe(fut.set_result, result)

        self._cmd_queue.put_nowait((callback, func, kwargs))
        return await fut

    def _smbconn_runner(self) -> NoReturn:
        """Runner(thread) that spins up an SMBConnection and processes tasks."""
        with SMBConnection(
            username=self._username,
            password=self._password,
            my_name=SERVICE_NAME,
            remote_name=self._remote_name,
            # choose sane default options but allow user to override them via the options dict
            domain=self._options.get("domain", ""),
            use_ntlm_v2=self._options.get("use_ntlm_v2", False),
            sign_options=self._options.get("sign_options", 2),
            is_direct_tcp=self._options.get("is_direct_tcp", False),
        ) as smb_conn:
            while True:
                callback, func, kwargs = self._cmd_queue.get()
                func = getattr(smb_conn, func)
                try:
                    result = func(**kwargs)
                except Exception as err:  # pylint: disable=broad-except
                    callback(None, err)
                else:
                    callback(result)
