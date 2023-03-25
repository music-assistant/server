"""SMB filesystem provider for Music Assistant."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from os.path import basename
from typing import TYPE_CHECKING

import smbclient
from smbclient import path as smbpath

from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.common.models.errors import LoginFailed
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.server.providers.filesystem_local.base import (
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    IGNORE_DIRS,
    FileSystemItem,
    FileSystemProviderBase,
)
from music_assistant.server.providers.filesystem_local.helpers import (
    get_absolute_path,
    get_relative_path,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CONF_HOST = "host"
CONF_SHARE = "share"
CONF_SUBFOLDER = "subfolder"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # silence logging a bit on smbprotocol
    logging.getLogger("smbprotocol").setLevel("WARNING")
    logging.getLogger("smbclient").setLevel("INFO")
    prov = SMBFileSystemProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key="host",
            type=ConfigEntryType.STRING,
            label="Remote host",
            required=True,
            description="The (fqdn) hostname of the SMB/CIFS server to connect to."
            "For example mynas.local.",
        ),
        ConfigEntry(
            key="share",
            type=ConfigEntryType.STRING,
            label="Share",
            required=True,
            description="The name of the share/service you'd like to connect to on "
            "the remote host, For example 'media'.",
        ),
        ConfigEntry(
            key="username",
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
            default_value="guest",
            description="The username to authenticate to the remote server. "
            "For anynymous access you may want to try with the user `guest`.",
        ),
        ConfigEntry(
            key="password",
            type=ConfigEntryType.SECURE_STRING,
            label="Username",
            required=True,
            default_value="guest",
            description="The username to authenticate to the remote server. "
            "For anynymous access you may want to try with the user `guest`.",
        ),
        ConfigEntry(
            key="subfolder",
            type=ConfigEntryType.STRING,
            label="Subfolder",
            required=False,
            default_value="",
            description="[optional] Use if your music is stored in a sublevel of the share. "
            "E.g. 'collections' or 'albums/A-K'.",
        ),
        CONF_ENTRY_MISSING_ALBUM_ARTIST,
    )


async def create_item(base_path: str, entry: smbclient.SMBDirEntry) -> FileSystemItem:
    """Create FileSystemItem from smbclient.SMBDirEntry."""

    def _create_item():
        entry_path = entry.path.replace("/\\", os.sep).replace("\\", os.sep)
        absolute_path = get_absolute_path(base_path, entry_path)
        stat = entry.stat(follow_symlinks=False)
        return FileSystemItem(
            name=entry.name,
            path=get_relative_path(base_path, entry_path),
            absolute_path=absolute_path,
            is_file=entry.is_file(follow_symlinks=False),
            is_dir=entry.is_dir(follow_symlinks=False),
            checksum=str(int(stat.st_mtime)),
            file_size=stat.st_size,
        )

    # run in thread because strictly taken this may be blocking IO
    return await asyncio.to_thread(_create_item)


class SMBFileSystemProvider(FileSystemProviderBase):
    """Implementation of an SMB File System Provider."""

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        # silence SMB.SMBConnection logger a bit
        logging.getLogger("SMB.SMBConnection").setLevel("WARNING")

        server: str = self.config.get_value(CONF_HOST)
        share: str = self.config.get_value(CONF_SHARE)
        subfolder: str = self.config.get_value(CONF_SUBFOLDER)

        # register smb session
        self.logger.info("Connecting to server %s", server)
        self._session = await asyncio.to_thread(
            smbclient.register_session,
            server,
            username=self.config.get_value(CONF_USERNAME),
            password=self.config.get_value(CONF_PASSWORD),
        )

        # create windows like path (\\server\share\subfolder)
        if subfolder.endswith(os.sep):
            subfolder = subfolder[:-1]
        self._root_path = f"{os.sep}{os.sep}{server}{os.sep}{share}{os.sep}{subfolder}"
        self.logger.debug("Using root path: %s", self._root_path)
        # validate provided path
        if not await asyncio.to_thread(smbpath.isdir, self._root_path):
            raise LoginFailed(f"Invalid share or subfolder given: {self._root_path}")

    async def listdir(
        self, path: str, recursive: bool = False
    ) -> AsyncGenerator[FileSystemItem, None]:
        """List contents of a given provider directory/path.

        Parameters
        ----------
        - path: path of the directory (relative or absolute) to list contents of.
            Empty string for provider's root.
        - recursive: If True will recursively keep unwrapping subdirectories (scandir equivalent).

        Returns:
        -------
            AsyncGenerator yielding FileSystemItem objects.

        """
        abs_path = get_absolute_path(self._root_path, path)
        for entry in await asyncio.to_thread(smbclient.scandir, abs_path):
            if entry.name.startswith(".") or any(x in entry.name for x in IGNORE_DIRS):
                # skip invalid/system files and dirs
                continue
            item = await create_item(self._root_path, entry)
            if recursive and item.is_dir:
                try:
                    async for subitem in self.listdir(item.absolute_path, True):
                        yield subitem
                except (OSError, PermissionError) as err:
                    self.logger.warning("Skip folder %s: %s", item.path, str(err))
            else:
                yield item

    async def resolve(
        self, file_path: str, require_local: bool = False  # noqa: ARG002
    ) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem.

        If require_local is True, we prefer to have the `local_path` attribute filled
        (e.g. with a tempfile), if supported by the provider/item.
        """
        file_path = file_path.replace("\\", os.sep)
        absolute_path = get_absolute_path(self._root_path, file_path)

        def _create_item():
            stat = smbclient.stat(absolute_path, follow_symlinks=False)
            return FileSystemItem(
                name=basename(file_path),
                path=get_relative_path(self._root_path, file_path),
                absolute_path=absolute_path,
                is_dir=smbpath.isdir(absolute_path),
                is_file=smbpath.isfile(absolute_path),
                checksum=str(int(stat.st_mtime)),
                file_size=stat.st_size,
            )

        # run in thread because strictly taken this may be blocking IO
        return await asyncio.to_thread(_create_item)

    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        if not file_path:
            return False  # guard
        file_path = file_path.replace("\\", os.sep)
        abs_path = get_absolute_path(self._root_path, file_path)
        return await asyncio.to_thread(smbpath.exists, abs_path)

    async def read_file_content(self, file_path: str, seek: int = 0) -> AsyncGenerator[bytes, None]:
        """Yield (binary) contents of file in chunks of bytes."""
        file_path = file_path.replace("\\", os.sep)
        abs_path = get_absolute_path(self._root_path, file_path)
        chunk_size = 512000
        queue = asyncio.Queue()
        self.logger.debug("Reading file contents for %s", abs_path)

        def _reader():
            with smbclient.open_file(abs_path, "rb", share_access="r") as _file:
                if seek:
                    _file.seek(seek)
                # yield chunks of data from file
                while True:
                    data = _file.read(chunk_size)
                    if not data:
                        break
                    self.mass.loop.call_soon_threadsafe(queue.put_nowait, data)
            self.mass.loop.call_soon_threadsafe(queue.put_nowait, b"")

        self.mass.create_task(_reader)
        while True:
            chunk = await queue.get()
            if chunk == b"":
                break
            yield chunk

    async def write_file_content(self, file_path: str, data: bytes) -> None:
        """Write entire file content as bytes (e.g. for playlists)."""
        file_path = file_path.replace("\\", os.sep)
        abs_path = get_absolute_path(self._root_path, file_path)

        def _writer():
            with smbclient.open_file(abs_path, "wb") as _file:
                _file.write(data)

        await asyncio.to_thread(_writer)
