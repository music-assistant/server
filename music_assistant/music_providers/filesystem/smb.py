"""SMB filesystem provider for Music Assistant."""


import asyncio
from typing import AsyncGenerator

from smb.base import SharedFile
from smb.SMBConnection import SMBConnection

from music_assistant.models.enums import ProviderType

from .base import FileSystemItem, FileSystemProviderBase
from .helpers import get_absolute_path, get_relative_path

SERVICE_NAME = "music_assistant"


async def create_item(base_path: str, entry: SharedFile) -> FileSystemItem:
    """Create FileSystemItem from smb.SharedFile."""

    def _create_item():
        absolute_path = get_absolute_path(base_path, entry.filename)
        return FileSystemItem(
            name=entry.name,
            path=get_relative_path(base_path, entry.filename),
            absolute_path=absolute_path,
            is_file=entry.isNormal,
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

    async def setup(self) -> bool:
        """Handle async initialization of the provider."""
        self._smb_connection = SMBConnection(
            self.config.username,
            self.config.password,
            SERVICE_NAME,
            self.config.target_name,
            use_ntlm_v2=True,
        )
        return await self.mass.loop.run_in_executor(
            None, self._smb_connection.connect, self.config.target_ip
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
        rel_path = get_relative_path(self._get_base_path, path) or "/"
        loop = asyncio.get_running_loop()
        path_result: list[SharedFile] = await loop.run_in_executor(
            None, self._smb_connection.listPath, self.config.share_name, rel_path
        )
        for entry in path_result:
            item = await create_item(self._get_base_path(), entry)
            if recursive and item.is_dir:
                try:
                    async for subitem in self.listdir(item.absolute_path, True):
                        yield subitem
                except (OSError, PermissionError) as err:
                    self.logger.warning("Skip folder %s: %s", item.path, str(err))
            elif item.is_file or item.is_dir:
                yield item

    async def resolve(self, file_path: str) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem."""
        raise NotImplementedError  # TODO !

    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        raise NotImplementedError  # TODO !

    async def read_file_content(self, file_path: str) -> bytes:
        """Read entire file content as bytes."""
        raise NotImplementedError  # TODO !

    async def iter_file_content(
        self, file_path: str, seek: int = 0, chunk_size: int = 64000
    ) -> AsyncGenerator[bytes, None]:
        """Yield (binary) contents of file in chunks of bytes."""
        raise NotImplementedError  # TODO !

    async def write_file_content(self, file_path: str, data: bytes) -> None:
        """Write entire file content as bytes (e.g. for playlists)."""
        raise NotImplementedError  # TODO !

    def _get_base_path(self):
        """Return the base path for this SMB provider."""
        return f"smb://{self.config.username}:{self.config.password}@{self.config.target_ip}/{self.config.share_name}"
