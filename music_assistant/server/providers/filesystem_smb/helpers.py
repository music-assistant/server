"""Some helpers for Filesystem based Musicproviders."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from io import BytesIO
from typing import Any

from smb.base import SharedFile, SMBTimeout
from smb.smb_structs import OperationFailure
from smb.SMBConnection import SMBConnection

from music_assistant.common.models.errors import LoginFailed

SERVICE_NAME = "music_assistant"


class AsyncSMB:
    """Async wrapped pysmb."""

    def __init__(
        self,
        remote_name: str,
        service_name: str,
        username: str,
        password: str,
        target_ip: str,
        options: dict[str, Any],
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
        return await asyncio.to_thread(self._conn.listPath, self._service_name, path)

    async def get_attributes(self, path: str) -> SharedFile:
        """Retrieve information about the file at *path* on the *service_name*."""
        return await asyncio.to_thread(self._conn.getAttributes, self._service_name, path)

    async def retrieve_file(self, path: str, offset: int = 0) -> AsyncGenerator[bytes, None]:
        """Retrieve file contents."""
        chunk_size = 256000
        while True:
            with BytesIO() as file_obj:
                await asyncio.to_thread(
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
        with BytesIO() as file_obj:
            file_obj.write(data)
            file_obj.seek(0)
            await asyncio.to_thread(
                self._conn.storeFile,
                self._service_name,
                path,
                file_obj,
            )

    async def path_exists(self, path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        try:
            await asyncio.to_thread(self._conn.getAttributes, self._service_name, path)
        except (OperationFailure, SMBTimeout):
            return False
        return True

    async def connect(self) -> None:
        """Connect to the SMB server."""
        try:
            assert await asyncio.to_thread(self._conn.connect, self._target_ip) is True
        except Exception as exc:
            raise LoginFailed(f"SMB Connect failed to {self._remote_name}") from exc

    async def __aenter__(self) -> AsyncSMB:
        """Enter context manager."""
        # connect
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        """Exit context manager."""
        self._conn.close()
