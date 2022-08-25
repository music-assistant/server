"""SMB filesystem provider for Music Assistant."""


import asyncio
import os
from io import BytesIO
from typing import AsyncGenerator

from smb.base import SharedFile
from smb.smb_structs import OperationFailure
from smb.SMBConnection import SMBConnection

from music_assistant.helpers.util import get_ip_from_host
from music_assistant.models.enums import ProviderType

from .base import FileSystemItem, FileSystemProviderBase
from .helpers import get_absolute_path, get_relative_path

SERVICE_NAME = "music_assistant"


async def create_item(
    file_path: str, entry: SharedFile, root_path: str
) -> FileSystemItem:
    """Create FileSystemItem from smb.SharedFile."""

    def _create_item():
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

    # run in executor because strictly taken this may be blocking IO
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _create_item)


class SMBFileSystemProvider(FileSystemProviderBase):
    """Implementation of an SMB File System Provider."""

    _attr_name = "smb"
    _attr_type = ProviderType.FILESYSTEM_SMB
    _smb_connection = None
    _service_name = ""
    _root_path = "/"

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""
        # extract params from path
        if self.config.path.startswith("\\\\"):
            path_parts = self.config.path[2:].split("\\", 2)
        elif self.config.path.startswith("smb://"):
            path_parts = self.config.path[6:].split("/", 2)
        else:
            path_parts = self.config.path.split(os.sep)
        remote_name = path_parts[0]
        self._service_name = path_parts[1]
        if len(path_parts) > 2:
            self._root_path = os.sep + path_parts[2]
        self._smb_connection = SMBConnection(
            username=self.config.username,
            password=self.config.password,
            my_name=SERVICE_NAME,
            remote_name=remote_name,
            # choose sane default options but allow user to override them via the options dict
            domain=self.config.options.get("domain", ""),
            use_ntlm_v2=self.config.options.get("use_ntlm_v2", False),
            sign_options=self.config.options.get("sign_options", 2),
            is_direct_tcp=self.config.options.get("is_direct_tcp", False),
        )
        default_target_ip = await get_ip_from_host(remote_name)
        target_ip = self.config.options.get("target_ip", default_target_ip)
        return await self.mass.loop.run_in_executor(
            None, self._smb_connection.connect, target_ip
        )

    async def listdir(
        self, path: str, recursive: bool = False
    ) -> AsyncGenerator[FileSystemItem, None]:
        """
        List contents of a given provider directory/path.

        Parameters:
            - path: path of the directory (relative or absolute) to list contents of.
              Empty string for provider's root.
            - recursive: If True will recursively keep unwrapping subdirectories (scandir equivalent).

        Returns:
            AsyncGenerator yielding FileSystemItem objects.

        """
        abs_path = get_absolute_path(self._root_path, path)
        path_result: list[SharedFile] = await self.mass.loop.run_in_executor(
            None, self._smb_connection.listPath, self._service_name, abs_path
        )
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
            elif item.is_file or item.is_dir:
                yield item

    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem."""
        absolute_path = get_absolute_path(self._root_path, file_path)
        entry: SharedFile = await self.mass.loop.run_in_executor(
            None,
            self._smb_connection.getAttributes,
            self._service_name,
            absolute_path,
        )
        return FileSystemItem(
            name=file_path,
            path=get_relative_path(self._root_path, file_path),
            absolute_path=absolute_path,
            is_file=not entry.isDirectory,
            is_dir=entry.isDirectory,
            checksum=str(int(entry.last_write_time)),
            file_size=entry.file_size,
        )

    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        try:
            await self.resolve(file_path)
        except OperationFailure:
            return False
        return True

    async def read_file_content(
        self, file_path: str, seek: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Yield (binary) contents of file in chunks of bytes."""
        abs_path = get_absolute_path(self._root_path, file_path)
        chunk_size = 256000

        def _read_chunk_from_file(offset: int):
            with BytesIO() as file_obj:
                self._smb_connection.retrieveFileFromOffset(
                    self._service_name,
                    abs_path,
                    file_obj=file_obj,
                    offset=offset,
                    max_length=chunk_size,
                )
                file_obj.seek(0)
                return file_obj.read()

        offset = seek
        while True:
            data = await self.mass.loop.run_in_executor(
                None, _read_chunk_from_file, offset
            )
            if not data:
                break
            yield data
            if len(data) < chunk_size:
                break
            offset += len(data)

    async def write_file_content(self, file_path: str, data: bytes) -> None:
        """Write entire file content as bytes (e.g. for playlists)."""
        raise NotImplementedError  # TODO !
