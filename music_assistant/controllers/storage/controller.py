"""
Storage controller: the storage locations the server can see.

Locations come from the mount table of the server process (container volumes, network shares
and drives), from the folders an admin registered on this server, and from the server's own
data and cache directories. A caller that manages every music source sees all of them; any
other caller sees only the media locations it may put a music source of its own on.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope
from music_assistant_models.errors import ActionUnavailable, InvalidDataError

from music_assistant.constants import CONF_STORAGE_FOLDERS
from music_assistant.controllers.storage.backends.mountinfo import (
    parse_mountinfo,
    parse_mountpoints,
    read_mountinfo,
)
from music_assistant.controllers.storage.constants import (
    CONTAINER_MARKER_FILES,
    DIR_SIZE_MAX_AGE,
    DIR_SIZES_TASK_ID,
    MAX_LISTED_FOLDERS,
    MEMBER_VISIBLE_KINDS,
    REFRESH_INTERVAL,
    REFRESH_TASK_ID,
)
from music_assistant.controllers.storage.models import (
    StorageInfo,
    StorageKind,
    StorageLocation,
    StorageUsage,
)
from music_assistant.controllers.streams.audio_analysis import FILESYSTEM_PROVIDER_DOMAINS
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_scope,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.util import get_folder_size
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant_models.config_entries import CoreConfig

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

READ_SCOPES = (Scope.CONFIG_PROVIDERS_OWN, Scope.CONFIG_PROVIDERS_READ)
BYTES_PER_GB = float(1 << 30)


class StorageController(CoreController):
    """Keeps track of the storage locations the server can see."""

    domain: str = "storage"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the storage controller."""
        super().__init__(mass)
        self.manifest.name = "Storage"
        self.manifest.description = "Keeps track of the storage the server can use."
        self.manifest.icon = "harddisk"
        self._locations: list[StorageLocation] = []
        self._in_container = False
        self._refresh_lock = asyncio.Lock()
        self._dir_sizes: dict[StorageUsage, float] = {}
        self._dir_sizes_requested: float | None = None

    @property
    def locations(self) -> list[StorageLocation]:
        """Return every known storage location, regardless of who may see it."""
        return self._locations

    @property
    def can_add_local_folder(self) -> bool:
        """Return whether a folder on this server can be registered as a media location."""
        # a folder of the host is only reachable from inside a container when it is mapped in
        # as a volume, which then shows up as a location of its own
        return not self._in_container

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self._in_container = self.mass.running_as_hass_addon or await asyncio.to_thread(
            _running_in_container
        )
        # the first refresh runs in the background: a mounted network share whose server is
        # gone can block filesystem calls for a long time, which must not hold up the startup
        self.mass.create_task(self._periodic_refresh(), task_id=REFRESH_TASK_ID)
        self._request_dir_sizes()

    async def close(self) -> None:
        """Handle logic on server stop."""
        self.mass.cancel_timer(REFRESH_TASK_ID)
        self.mass.cancel_task(REFRESH_TASK_ID)
        self.mass.cancel_task(DIR_SIZES_TASK_ID)

    @api_command("storage/info", required_scope=READ_SCOPES)
    async def get_info(self) -> StorageInfo:
        """Return the storage locations the caller may see and what can be added."""
        manages_all_sources = _caller_manages_all_sources()
        if manages_all_sources:
            # the used space is only shown on the data and cache rows these callers see
            self._request_dir_sizes()
        return StorageInfo(
            locations=self.get_locations(manages_all_sources),
            can_mount_shares=False,
            mount_backend=None,
            supported_share_types=[],
            can_add_local_folder=self.can_add_local_folder,
        )

    @api_command("storage/folders", required_scope=READ_SCOPES)
    async def get_folders(self, path: str) -> list[str]:
        """
        Return the names of the subfolders of a folder, sorted.

        :param path: A media location the caller may see, or a folder inside one.
        """
        return await self.list_folders(path, _caller_manages_all_sources())

    @api_command("storage/local_folders/add", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def add_local_folder(self, path: str) -> StorageLocation:
        """
        Register a folder on this server as a media location.

        Only possible when the server does not run in a container.

        :param path: Absolute path of an existing folder.
        """
        if not self.can_add_local_folder:
            msg = "A folder can not be added when the server runs in a container"
            raise ActionUnavailable(
                msg,
                translation_key="local_folder_not_allowed",
                translation_owner=self.translation_owner,
            )
        path = os.path.normpath(path)
        if not Path(path).is_absolute():
            msg = f"Not an absolute path: {path}"
            raise InvalidDataError(
                msg,
                translation_key="folder_path_not_absolute",
                translation_owner=self.translation_owner,
            )
        if not await asyncio.to_thread(Path(path).is_dir):
            raise self._folder_not_found(path)
        folders = self._get_registered_folders()
        if path not in folders:
            self.mass.config.set(CONF_STORAGE_FOLDERS, [*folders, path], immediate=True)
        await self.refresh()
        return next(loc for loc in self._locations if loc.path == path and loc.managed)

    @api_command("storage/local_folders/remove", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def remove_local_folder(self, path: str) -> None:
        """
        Remove a registered folder from the media locations.

        Refused while a music source uses the folder.

        :param path: Path of the registered folder.
        """
        path = os.path.normpath(path)
        folders = self._get_registered_folders()
        if path not in folders:
            msg = f"Not a registered folder: {path}"
            raise InvalidDataError(
                msg,
                translation_key="folder_not_registered",
                translation_owner=self.translation_owner,
            )
        if (source := self._get_source_using(path)) is not None:
            msg = f"{source.name} uses {path}"
            raise ActionUnavailable(
                msg,
                translation_key="location_in_use",
                translation_owner=self.translation_owner,
                translation_args=[source.name],
            )
        self.mass.config.set(
            CONF_STORAGE_FOLDERS, [folder for folder in folders if folder != path], immediate=True
        )
        await self.refresh()

    def get_locations(self, manages_all_sources: bool = True) -> list[StorageLocation]:
        """
        Return the storage locations a caller may see.

        :param manages_all_sources: Whether the caller manages every music source; any other
            caller only sees the media locations it may put a music source of its own on.
        """
        if manages_all_sources:
            return list(self._locations)
        return [
            loc
            for loc in self._locations
            if loc.usage == StorageUsage.MEDIA and loc.kind in MEMBER_VISIBLE_KINDS
        ]

    def get_location_for_path(self, path: str) -> StorageLocation | None:
        """
        Return the most specific storage location that contains a path.

        :param path: An absolute path.
        """
        return max(
            (loc for loc in self._locations if _is_within(path, loc.path)),
            key=lambda loc: len(loc.path),
            default=None,
        )

    async def is_available(self, path: str) -> bool:
        """
        Return whether a folder can be used right now.

        A folder in a location backed by a mount is only available while that mount is there,
        so the empty directory an unmounted share leaves behind does not count.

        :param path: An absolute path.
        """
        location = self.get_location_for_path(path)
        mountpoint = location.mountpoint if location is not None else None
        return await asyncio.to_thread(_is_available, path, mountpoint)

    async def list_folders(self, path: str, manages_all_sources: bool = True) -> list[str]:
        """
        Return the names of the subfolders of a folder in a media location, sorted.

        Hidden folders and symlinks are left out and at most 500 names are returned.

        :param path: A media location the caller may see, or a folder inside one.
        :param manages_all_sources: Whether the caller manages every music source.
        """
        path = os.path.normpath(path)
        roots = [
            loc.path
            for loc in self.get_locations(manages_all_sources)
            if loc.usage == StorageUsage.MEDIA and _is_within(path, loc.path)
        ]
        # the path must also stay inside a location once its symlinks are resolved
        real_path = await asyncio.to_thread(_resolve_within, path, roots) if roots else None
        if real_path is None:
            msg = f"Not inside a storage location: {path}"
            raise InvalidDataError(
                msg,
                translation_key="path_not_allowed",
                translation_owner=self.translation_owner,
            )
        try:
            return await asyncio.to_thread(_list_subfolders, real_path)
        except FileNotFoundError, NotADirectoryError:
            raise self._folder_not_found(path) from None
        except OSError as err:
            # e.g. a network share whose server is gone, or a folder without read permission
            msg = f"Can not read {path}: {err}"
            raise ActionUnavailable(
                msg,
                translation_key="folder_unreadable",
                translation_owner=self.translation_owner,
                translation_args=[path],
            ) from err

    async def refresh(self) -> None:
        """Rebuild the list of storage locations."""
        async with self._refresh_lock:
            self._locations = await asyncio.to_thread(
                _build_locations,
                data_path=self.mass.storage_path,
                cache_path=self.mass.cache_path,
                folders=self._get_registered_folders(),
                in_container=self._in_container,
                supervisor=self.mass.running_as_hass_addon,
                dir_sizes=dict(self._dir_sizes),
            )

    async def _periodic_refresh(self) -> None:
        """Refresh the storage locations and schedule the next refresh."""
        try:
            await self.refresh()
        except Exception:
            self.logger.exception("Failed to refresh the storage locations")
        self.mass.call_later(REFRESH_INTERVAL, self._periodic_refresh, task_id=REFRESH_TASK_ID)

    def _request_dir_sizes(self) -> None:
        """Measure the data and cache directories when the last measurement is outdated."""
        now = time.monotonic()
        if self._dir_sizes_requested is not None and (
            now - self._dir_sizes_requested < DIR_SIZE_MAX_AGE
        ):
            return
        self._dir_sizes_requested = now
        self.mass.create_task(self._update_dir_sizes(), task_id=DIR_SIZES_TASK_ID)

    async def _update_dir_sizes(self) -> None:
        """Measure the data and cache directories and show the result on their rows."""
        self._dir_sizes = {
            StorageUsage.DATA: round(await get_folder_size(self.mass.storage_path), 2),
            StorageUsage.CACHE: round(await get_folder_size(self.mass.cache_path), 2),
        }
        for location in self._locations:
            if location.usage != StorageUsage.MEDIA:
                location.used_space_gb = self._dir_sizes.get(location.usage)

    def _get_registered_folders(self) -> list[str]:
        """Return the folders registered as a media location."""
        return list(self.mass.config.get(CONF_STORAGE_FOLDERS, []))

    def _get_source_using(self, path: str) -> ProviderInstanceType | None:
        """
        Return a loaded music source that reads its files from a path or a folder inside it.

        :param path: An absolute path.
        """
        for provider in self.mass.providers:
            if provider.domain not in FILESYSTEM_PROVIDER_DOMAINS:
                continue
            base_path = getattr(provider, "base_path", None)
            if isinstance(base_path, str) and _is_within(base_path, path):
                return provider
        return None

    def _folder_not_found(self, path: str) -> InvalidDataError:
        """
        Return the error for a folder that does not exist.

        :param path: The folder that does not exist.
        """
        return InvalidDataError(
            f"Folder does not exist: {path}",
            translation_key="folder_not_found",
            translation_owner=self.translation_owner,
            translation_args=[path],
        )


def _caller_manages_all_sources() -> bool:
    """Return whether the caller of an API command manages every music source."""
    user = get_current_user()
    # no user context means an internal (server-side) caller, which is trusted
    return user is None or has_scope(user, Scope.CONFIG_PROVIDERS_WRITE)


def _is_within(path: str, base: str) -> bool:
    """Return whether an absolute path is a base path or lies below it, lexically."""
    return Path(path).is_absolute() and is_safe_path(path, base)


def _running_in_container() -> bool:
    """Return whether the server runs in a Docker or Podman container (blocking)."""
    return any(Path(marker).exists() for marker in CONTAINER_MARKER_FILES)


def _build_locations(
    *,
    data_path: str,
    cache_path: str,
    folders: list[str],
    in_container: bool,
    supervisor: bool,
    dir_sizes: dict[StorageUsage, float],
) -> list[StorageLocation]:
    """Build the storage locations the server can see (blocking)."""
    data_path = os.path.normpath(data_path)
    cache_path = os.path.normpath(cache_path)
    media: dict[str, StorageLocation] = {}
    for mount in parse_mountinfo(
        read_mountinfo(),
        excluded_paths=(data_path, cache_path),
        in_container=in_container,
        supervisor=supervisor,
    ):
        if (is_dir := _is_dir(mount.mountpoint)) is False:
            # a file bound into a container
            continue
        media[mount.mountpoint] = _build_location(
            mount.mountpoint,
            "Media folder" if mount.kind == StorageKind.BUILTIN_MEDIA else None,
            StorageUsage.MEDIA,
            mount.kind,
            available=bool(is_dir),
            read_only=mount.read_only,
            fstype=mount.fstype,
            mountpoint=mount.mountpoint,
        )
    # a registered folder replaces a discovered location on the same path
    for folder in folders:
        media[folder] = _build_location(
            folder,
            None,
            StorageUsage.MEDIA,
            StorageKind.MANUAL,
            available=bool(_is_dir(folder)),
            managed=True,
        )
    server_kind = StorageKind.CONTAINER_VOLUME if in_container else StorageKind.LOCAL_DISK
    return [
        *sorted(media.values(), key=lambda loc: loc.path.casefold()),
        *(
            _build_location(
                path,
                name,
                usage,
                server_kind,
                available=bool(_is_dir(path)),
                used_space_gb=dir_sizes.get(usage),
            )
            for path, name, usage in (
                (data_path, "Data", StorageUsage.DATA),
                (cache_path, "Cache", StorageUsage.CACHE),
            )
        ),
    ]


def _build_location(
    path: str,
    name: str | None,
    usage: StorageUsage,
    kind: StorageKind,
    *,
    available: bool,
    read_only: bool = False,
    managed: bool = False,
    fstype: str | None = None,
    mountpoint: str | None = None,
    used_space_gb: float | None = None,
) -> StorageLocation:
    """Build a storage location, with the free space of its filesystem when available (blocking)."""
    free_space_gb = total_space_gb = None
    if available:
        try:
            usage_info = shutil.disk_usage(path)
        except OSError:
            pass
        else:
            free_space_gb = round(usage_info.free / BYTES_PER_GB, 2)
            total_space_gb = round(usage_info.total / BYTES_PER_GB, 2)
    return StorageLocation(
        path=path,
        name=name or Path(path).name or path,
        usage=usage,
        kind=kind,
        available=available,
        read_only=read_only,
        managed=managed,
        fstype=fstype,
        mountpoint=mountpoint,
        free_space_gb=free_space_gb,
        total_space_gb=total_space_gb,
        used_space_gb=used_space_gb,
    )


def _is_dir(path: str) -> bool | None:
    """Return whether a path is a directory, None when it can not be reached (blocking)."""
    try:
        return stat.S_ISDIR(Path(path).stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _is_available(path: str, mountpoint: str | None) -> bool:
    """Return whether a folder is there, and its mount when it has one (blocking)."""
    # the mount table rather than os.path.ismount, which misses a bind mount of a folder on the
    # filesystem it is mounted on
    if mountpoint is not None and mountpoint not in parse_mountpoints(read_mountinfo()):
        return False
    return Path(path).is_dir()


def _resolve_within(path: str, roots: list[str]) -> str | None:
    """Return the real path of a path when it lies within one of the roots, else None (blocking)."""
    real_path = os.path.realpath(path)
    if any(is_safe_path(real_path, os.path.realpath(root)) for root in roots):
        return real_path
    return None


def _list_subfolders(path: str) -> list[str]:
    """Return the sorted visible subfolders of a folder (blocking)."""
    with os.scandir(path) as entries:
        names = [
            entry.name
            for entry in entries
            if not entry.name.startswith(".")
            and entry.is_dir(follow_symlinks=False)
            and _is_valid_utf8(entry.name)
        ]
    return sorted(names, key=str.casefold)[:MAX_LISTED_FOLDERS]


def _is_valid_utf8(name: str) -> bool:
    """Return whether a file name can be sent to a client as is."""
    try:
        name.encode()
    except UnicodeEncodeError:
        return False
    return True
