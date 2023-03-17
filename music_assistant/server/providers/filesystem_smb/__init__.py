"""SMB filesystem provider for Music Assistant."""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from smb.base import SharedFile

from music_assistant.common.helpers.util import get_ip_from_host
from music_assistant.common.models.errors import LoginFailed
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.server.controllers.cache import use_cache
from music_assistant.server.providers.filesystem_local.base import (
    FileSystemItem,
    FileSystemProviderBase,
)
from music_assistant.server.providers.filesystem_local.helpers import (
    get_absolute_path,
    get_relative_path,
)

from .helpers import AsyncSMB

CONF_HOST = "host"
CONF_SHARE = "share"
CONF_SUBFOLDER = "subfolder"


async def create_item(file_path: str, entry: SharedFile, root_path: str) -> FileSystemItem:
    """Create FileSystemItem from smb.SharedFile."""
    rel_path = get_relative_path(root_path, file_path)
    abs_path = get_absolute_path(root_path, file_path)
    return FileSystemItem(
        name=entry.filename,
        path=rel_path,
        absolute_path=abs_path,
        is_file=not entry.isDirectory,
        is_dir=entry.isDirectory,
        checksum=str(int(entry.last_write_time)),
        file_size=entry.file_size,
    )


class SMBFileSystemProvider(FileSystemProviderBase):
    """Implementation of an SMB File System Provider."""

    _service_name = ""
    _root_path = "/"
    _remote_name = ""
    _target_ip = ""

    async def setup(self) -> None:
        """Handle async initialization of the provider."""
        # silence SMB.SMBConnection logger a bit
        logging.getLogger("SMB.SMBConnection").setLevel("WARNING")

        self._remote_name = self.config.get_value(CONF_HOST)
        self._service_name = self.config.get_value(CONF_SHARE)

        # validate provided path
        subfolder: str = self.config.get_value(CONF_SUBFOLDER)
        subfolder.replace("\\", "/")
        if not subfolder.startswith("/"):
            subfolder = "/" + subfolder
        if not subfolder.endswith("/"):
            subfolder += "/"
        self._root_path = subfolder

        # resolve dns name to IP
        target_ip = await get_ip_from_host(self._remote_name)
        if target_ip is None:
            raise LoginFailed(
                f"Unable to resolve {self._remote_name}, maybe use an IP address as remote host ?"
            )
        self._target_ip = target_ip

        # test connection and return
        # this code will raise if the connection did not succeed
        async with self._get_smb_connection():
            return

    async def listdir(
        self,
        path: str,
        recursive: bool = False,
    ) -> AsyncGenerator[FileSystemItem, None]:
        """List contents of a given provider directory/path.

        Parameters
        ----------
        - path: path of the directory (relative or absolute) to list contents of.
            Empty string for provider's root.
        - recursive: If True will recursively keep unwrapping subdirectories (scandir equivalent)

        Returns:
        -------
            AsyncGenerator yielding FileSystemItem objects.

        """
        abs_path = get_absolute_path(self._root_path, path)
        async with self._get_smb_connection() as smb_conn:
            path_result: list[SharedFile] = await smb_conn.list_path(abs_path)

        for entry in path_result:
            if entry.filename.startswith("."):
                # skip invalid/system files and dirs
                continue
            file_path = os.path.join(path, entry.filename)
            item = await create_item(file_path, entry, self._root_path)
            if recursive and item.is_dir:
                # yield sublevel recursively
                try:
                    async for subitem in self.listdir(file_path, True):
                        yield subitem
                except (OSError, PermissionError) as err:
                    self.logger.warning("Skip folder %s: %s", item.path, str(err))
            else:
                # yield single item (file or directory)
                yield item

    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem."""
        abs_path = get_absolute_path(self._root_path, file_path)
        async with self._get_smb_connection() as smb_conn:
            entry: SharedFile = await smb_conn.get_attributes(abs_path)
            return FileSystemItem(
                name=file_path,
                path=get_relative_path(self._root_path, file_path),
                absolute_path=abs_path,
                is_file=not entry.isDirectory,
                is_dir=entry.isDirectory,
                checksum=str(int(entry.last_write_time)),
                file_size=entry.file_size,
            )

    @use_cache(15 * 60)
    async def exists(self, file_path: str) -> bool:
        """Return bool if this FileSystem musicprovider has given file/dir."""
        abs_path = get_absolute_path(self._root_path, file_path)
        async with self._get_smb_connection() as smb_conn:
            return await smb_conn.path_exists(abs_path)

    async def read_file_content(self, file_path: str, seek: int = 0) -> AsyncGenerator[bytes, None]:
        """Yield (binary) contents of file in chunks of bytes."""
        abs_path = get_absolute_path(self._root_path, file_path)

        async with self._get_smb_connection() as smb_conn:
            async for chunk in smb_conn.retrieve_file(abs_path, seek):
                yield chunk

    async def write_file_content(self, file_path: str, data: bytes) -> None:
        """Write entire file content as bytes (e.g. for playlists)."""
        abs_path = get_absolute_path(self._root_path, file_path)
        async with self._get_smb_connection() as smb_conn:
            await smb_conn.write_file(abs_path, data)

    @asynccontextmanager
    async def _get_smb_connection(self) -> AsyncGenerator[AsyncSMB, None]:
        """Get instance of AsyncSMB."""
        # For now we just create a connection per call
        # as that is the most reliable (but a bit slower)
        # this could be improved by creating a connection pool
        # if really needed

        async with AsyncSMB(
            remote_name=self._remote_name,
            service_name=self._service_name,
            username=self.config.get_value(CONF_USERNAME),
            password=self.config.get_value(CONF_PASSWORD),
            target_ip=self._target_ip,
            use_ntlm_v2=self.config.get_value("use_ntlm_v2"),
            sign_options=self.config.get_value("sign_options"),
            is_direct_tcp=self.config.get_value("is_direct_tcp"),
        ) as smb_conn:
            yield smb_conn
