"""Some helpers for Filesystem based Musicproviders."""
from __future__ import annotations

import asyncio
import os
from io import BytesIO
from typing import Any, AsyncGenerator, Dict

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
        self._conn = SMBConnection(
            username=self._username,
            password=self._password,
            my_name=SERVICE_NAME,
            remote_name=self._remote_name,
            # choose sane default options but allow user to override them via the options dict
            domain=options.get("domain", ""),
            use_ntlm_v2=options.get("use_ntlm_v2", False),
            sign_options=options.get("sign_options", 2),
            is_direct_tcp=options.get("is_direct_tcp", False),
        )

    async def list_path(self, path: str) -> list[SharedFile]:
        """Retrieve a directory listing of files/folders at *path*."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._conn.listPath, self._service_name, path
        )

    async def get_attributes(self, path: str) -> SharedFile:
        """Retrieve information about the file at *path* on the *service_name*."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._conn.getAttributes, self._service_name, path
        )

    async def retrieve_file(
        self, path: str, offset: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Retrieve file contents."""
        loop = asyncio.get_running_loop()

        chunk_size = 256000
        while True:
            with BytesIO() as file_obj:
                await loop.run_in_executor(
                    None,
                    self._conn.retrieveFileFromOffset,
                    self._service_name,
                    path,
                    file_obj,
                    offset,
                    chunk_size,
                )
                file_obj.seek(0)
                chunk = file_obj.read()
                yield chunk
                offset += len(chunk)
                if len(chunk) < chunk_size:
                    break

    async def write_file(self, path: str, data: bytes) -> SharedFile:
        """Store the contents to the file at *path*."""
        loop = asyncio.get_running_loop()
        with BytesIO() as file_obj:
            file_obj.write(data)
            file_obj.seek(0)
            await loop.run_in_executor(
                None,
                self._conn.storeFile,
                self._service_name,
                path,
                file_obj,
            )

    async def path_exists(self, path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, self._conn.getAttributes, self._service_name, path
            )
        except (OperationFailure, SMBTimeout):
            return False
        return True

    async def connect(self) -> None:
        """Connect to the SMB server."""
        loop = asyncio.get_running_loop()
        try:
            assert (
                await loop.run_in_executor(None, self._conn.connect, self._target_ip)
                is True
            )
        except Exception as exc:
            raise LoginFailed(f"SMB Connect failed to {self._remote_name}") from exc

    async def __aenter__(self) -> "AsyncSMB":
        """Enter context manager."""
        # connect
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self._conn.close()
