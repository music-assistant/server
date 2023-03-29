"""SMB filesystem provider for Music Assistant."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import suppress
from os.path import basename
from typing import TYPE_CHECKING

import smbclient
from smbclient import path as smbpath

from music_assistant.common.helpers.util import empty_queue, get_ip_from_host
from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.common.models.errors import LoginFailed
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.server.controllers.cache import use_cache
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
CONF_CONN_LIMIT = "connection_limit"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # silence logging a bit on smbprotocol
    logging.getLogger("smbprotocol").setLevel("WARNING")
    logging.getLogger("smbclient").setLevel("INFO")
    # check if valid dns name is given
    server: str = config.get_value(CONF_HOST)
    if not await get_ip_from_host(server):
        raise LoginFailed(f"Unable to resolve {server}, make sure the address is resolveable.")
    prov = SMBFileSystemProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_HOST,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The (fqdn) hostname of the SMB/CIFS/DFS server to connect to."
            "For example mynas.local.",
        ),
        ConfigEntry(
            key=CONF_SHARE,
            type=ConfigEntryType.STRING,
            label="Share",
            required=True,
            description="The name of the share/service you'd like to connect to on "
            "the remote host, For example 'media'.",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
            default_value="guest",
            description="The username to authenticate to the remote server. "
            "For anynymous access you may want to try with the user `guest`.",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            default_value=None,
            description="The username to authenticate to the remote server. "
            "For anynymous access you may want to try with the user `guest`.",
        ),
        ConfigEntry(
            key=CONF_SUBFOLDER,
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
        server: str = self.config.get_value(CONF_HOST)
        share: str = self.config.get_value(CONF_SHARE)
        subfolder: str = self.config.get_value(CONF_SUBFOLDER)

        # create windows like path (\\server\share\subfolder)
        if subfolder.endswith(os.sep):
            subfolder = subfolder[:-1]
        subfolder = subfolder.replace("\\", os.sep).replace("/", os.sep)
        self._root_path = f"{os.sep}{os.sep}{server}{os.sep}{share}{os.sep}{subfolder}"
        self.logger.debug("Using root path: %s", self._root_path)

        # register smb session
        self.logger.info("Connecting to server %s", server)
        try:
            self._session = await asyncio.to_thread(
                smbclient.register_session,
                server,
                username=self.config.get_value(CONF_USERNAME),
                password=self.config.get_value(CONF_PASSWORD),
            )
            # validate provided path
            if not await asyncio.to_thread(smbpath.isdir, self._root_path):
                raise LoginFailed(f"Invalid subfolder given: {subfolder}")
        except Exception as err:
            if "Unable to negotiate " in str(err):
                detail = "Invalid credentials"
            elif "refused " in str(err):
                detail = "Invalid hostname (or host not reachable)"
            elif "STATUS_NOT_FOUND" in str(err):
                detail = "Share does not exist"
            elif "Invalid argument" in str(err) and "." not in server:
                detail = "Make sure to enter a FQDN hostname or IP-address"
            else:
                detail = str(err)
            raise LoginFailed(f"Connection failed for the given details: {detail}") from err

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
        self.logger.debug("Processing: %s", abs_path)
        entries = await asyncio.to_thread(smbclient.scandir, abs_path)
        for entry in entries:
            if entry.name.startswith(".") or any(x in entry.name for x in IGNORE_DIRS):
                # skip invalid/system files and dirs
                continue
            item = await create_item(self._root_path, entry)
            if recursive and item.is_dir:
                async for subitem in self.listdir(item.absolute_path, True):
                    yield subitem
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

    @use_cache(120)
    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        if not file_path:
            return False  # guard
        file_path = file_path.replace("\\", os.sep)
        abs_path = get_absolute_path(self._root_path, file_path)
        try:
            return await asyncio.to_thread(smbpath.exists, abs_path)
        except Exception as err:
            if "STATUS_OBJECT_NAME_INVALID" in str(err):
                return False
            raise err

    async def read_file_content(self, file_path: str, seek: int = 0) -> AsyncGenerator[bytes, None]:
        """Yield (binary) contents of file in chunks of bytes."""
        file_path = file_path.replace("\\", os.sep)
        absolute_path = get_absolute_path(self._root_path, file_path)

        queue = asyncio.Queue(1)

        def _reader():
            self.logger.debug("Reading file contents for %s", absolute_path)
            try:
                chunk_size = 64000
                bytes_sent = 0
                with smbclient.open_file(
                    absolute_path, "rb", buffering=chunk_size, share_access="r"
                ) as _file:
                    if seek:
                        _file.seek(seek)
                    while True:
                        chunk = _file.read(chunk_size)
                        if not chunk:
                            return
                        asyncio.run_coroutine_threadsafe(queue.put(chunk), self.mass.loop).result()
                        bytes_sent += len(chunk)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(b""), self.mass.loop).result()
                self.logger.debug(
                    "Finished Reading file contents for %s - bytes transferred: %s",
                    absolute_path,
                    bytes_sent,
                )

        try:
            task = self.mass.create_task(_reader)

            while True:
                chunk = await queue.get()
                if not chunk:
                    break
                yield chunk
        finally:
            empty_queue(queue)
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            del queue

    async def write_file_content(self, file_path: str, data: bytes) -> None:
        """Write entire file content as bytes (e.g. for playlists)."""
        file_path = file_path.replace("\\", os.sep)
        abs_path = get_absolute_path(self._root_path, file_path)

        def _writer():
            with smbclient.open_file(abs_path, "wb") as _file:
                _file.write(data)

        await asyncio.to_thread(_writer)
