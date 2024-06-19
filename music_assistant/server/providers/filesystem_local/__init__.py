"""Filesystem musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import os
import os.path
import re
from typing import TYPE_CHECKING

import aiofiles
import cchardet
import shortuuid
from aiofiles.os import wrap

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.common.models.errors import SetupFailedError
from music_assistant.constants import CONF_PATH

from .base import (
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    IGNORE_DIRS,
    FileSystemItem,
    FileSystemProviderBase,
)
from .helpers import get_absolute_path, get_relative_path

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


listdir = wrap(os.listdir)
isdir = wrap(os.path.isdir)
isfile = wrap(os.path.isfile)
exists = wrap(os.path.exists)
makedirs = wrap(os.makedirs)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    conf_path = config.get_value(CONF_PATH)
    if not await isdir(conf_path):
        msg = f"Music Directory {conf_path} does not exist"
        raise SetupFailedError(msg)
    prov = LocalFileSystemProvider(mass, manifest, config)
    prov.base_path = config.get_value(CONF_PATH)
    await prov.check_write_access()
    mass.call_later(30, prov.migrate_playlists)
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(key="path", type=ConfigEntryType.STRING, label="Path", default_value="/media"),
        CONF_ENTRY_MISSING_ALBUM_ARTIST,
    )


def sorted_scandir(base_path: str, sub_path: str) -> list[os.DirEntry]:
    """Implement os.scandir that returns (naturally) sorted entries."""

    def nat_key(name: str) -> tuple[int, str]:
        """Sort key for natural sorting."""
        return tuple(int(s) if s.isdigit() else s for s in re.split(r"(\d+)", name))

    def create_item(entry: os.DirEntry):
        """Create FileSystemItem from os.DirEntry."""
        absolute_path = get_absolute_path(base_path, entry.path)
        stat = entry.stat(follow_symlinks=False)
        return FileSystemItem(
            filename=entry.name,
            path=get_relative_path(base_path, entry.path),
            absolute_path=absolute_path,
            is_file=entry.is_file(follow_symlinks=False),
            is_dir=entry.is_dir(follow_symlinks=False),
            checksum=str(int(stat.st_mtime)),
            file_size=stat.st_size,
            # local filesystem is always local resolvable
            local_path=absolute_path,
        )

    return sorted(
        # filter out invalid dirs and hidden files
        [
            create_item(x)
            for x in os.scandir(sub_path)
            if x.name not in IGNORE_DIRS and not x.name.startswith(".")
        ],
        # sort by (natural) name
        key=lambda x: nat_key(x.name),
    )


class LocalFileSystemProvider(FileSystemProviderBase):
    """Implementation of a musicprovider for local files."""

    base_path: str

    async def check_write_access(self) -> None:
        """Perform check if we have write access."""
        # verify write access to determine we have playlist create/edit support
        # overwrite with provider specific implementation if needed
        temp_file_name = get_absolute_path(self.base_path, f"{shortuuid.random(8)}.txt")
        try:
            await self.write_file_content(temp_file_name, b"")
            await asyncio.to_thread(os.remove, temp_file_name)
            self.write_access = True
        except Exception as err:
            self.logger.debug("Write access disabled: %s", str(err))

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
        abs_path = get_absolute_path(self.base_path, path)
        for entry in await asyncio.to_thread(sorted_scandir, self.base_path, abs_path):
            if recursive and entry.is_dir:
                try:
                    async for subitem in self.listdir(entry.absolute_path, True):
                        yield subitem
                except (OSError, PermissionError) as err:
                    self.logger.warning("Skip folder %s: %s", entry.path, str(err))
            else:
                yield entry

    async def resolve(
        self,
        file_path: str,
        require_local: bool = False,
    ) -> FileSystemItem:
        """Resolve (absolute or relative) path to FileSystemItem.

        If require_local is True, we prefer to have the `local_path` attribute filled
        (e.g. with a tempfile), if supported by the provider/item.
        """
        absolute_path = get_absolute_path(self.base_path, file_path)

        def _create_item():
            stat = os.stat(absolute_path, follow_symlinks=False)
            return FileSystemItem(
                filename=os.path.basename(file_path),
                path=get_relative_path(self.base_path, file_path),
                absolute_path=absolute_path,
                is_dir=os.path.isdir(absolute_path),
                is_file=os.path.isfile(absolute_path),
                checksum=str(int(stat.st_mtime)),
                file_size=stat.st_size,
                # local filesystem is always local resolvable
                local_path=absolute_path,
            )

        # run in thread because strictly taken this may be blocking IO
        return await asyncio.to_thread(_create_item)

    async def exists(self, file_path: str) -> bool:
        """Return bool is this FileSystem musicprovider has given file/dir."""
        if not file_path:
            return False  # guard
        abs_path = get_absolute_path(self.base_path, file_path)
        return await exists(abs_path)

    async def read_file_content(self, file_path: str, seek: int = 0) -> AsyncGenerator[bytes, None]:
        """Yield (binary) contents of file in chunks of bytes."""
        abs_path = get_absolute_path(self.base_path, file_path)
        chunk_size = 64000
        async with aiofiles.open(abs_path, "rb") as _file:
            if seek:
                await _file.seek(seek)
            # yield chunks of data from file
            while True:
                data = await _file.read(chunk_size)
                if not data:
                    break
                yield data

    async def write_file_content(self, file_path: str, data: bytes) -> None:
        """Write entire file content as bytes (e.g. for playlists)."""
        abs_path = get_absolute_path(self.base_path, file_path)
        async with aiofiles.open(abs_path, "wb") as _file:
            await _file.write(data)

    async def migrate_playlists(self) -> None:
        """Migrate Music Assistant filesystem playlists."""
        # Remove this code when 2.0 stable has been released!
        # prior to version 2.0.0b137 Music Assistant stored universal playlists
        # in the filesystem (root of the music dir, m3u files with uri's)
        # that is converted into a universal builtin provider approach in b137
        # so the filesystem is not longer polluted/abused for this.
        # this code hunts these playlists, migrates them to the universal provider
        # and cleans up the files.
        cache_key = f"{self.instance_id}.playlist_migration_done"
        if await self.mass.cache.get(cache_key):
            return
        async for item in self.listdir("", False):
            if not item.is_file:
                continue
            if item.ext != "m3u":
                continue
            playlist_data = b""
            async for chunk in self.read_file_content(item.absolute_path):
                playlist_data += chunk
            encoding_details = await asyncio.to_thread(cchardet.detect, playlist_data)
            playlist_data = playlist_data.decode(encoding_details["encoding"] or "utf-8")
            # a (legacy) playlist file created by MA does not have EXTINFO tags and has uri's
            if "EXTINF" in playlist_data or "://" not in playlist_data:
                continue
            all_uris: list[str] = []
            skipped_lines = 0
            for playlist_line in playlist_data.split("\n"):
                playlist_line = playlist_line.strip()  # noqa: PLW2901
                if not playlist_line:
                    continue
                if "://" not in playlist_line:
                    skipped_lines += 1
                    self.logger.debug("Ignoring line in migration playlist: %s", playlist_line)
                all_uris.append(playlist_line)
            if skipped_lines > len(all_uris):
                self.logger.warning("NOT migrating playlist: %s", item.path)
                continue
            # create playlist on the builtin provider
            new_playlist = await self.mass.music.playlists.create_playlist(item.name, "builtin")
            # append existing uri's to the new playlist
            await self.mass.music.playlists.add_playlist_tracks(new_playlist.item_id, all_uris)
            # remove existing item from the library
            if library_item := await self.mass.music.playlists.get_library_item_by_prov_id(
                item.path, self.instance_id
            ):
                await self.mass.music.playlists.remove_item_from_library(library_item.item_id)
            # remove old file
            await asyncio.to_thread(os.remove, item.absolute_path)
            # refresh the playlist so it builds the metadata
            await self.mass.music.playlists.add_item_to_library(new_playlist, metadata_lookup=True)
            self.logger.info("Migrated playlist %s", item.name)
        await self.mass.cache.set(cache_key, True, expiration=365 * 86400)
