"""
Storage controller: the storage locations the server can see.

Locations come from the mount table of the server process (container volumes, network shares
and drives), from the network shares and folders an admin added, and from the server's own
data and cache directories. A caller that manages every music source sees all of them; any
other caller sees the media locations that were deliberately made available to the server.

The list follows the mount table without touching any location, so an idle network share is
left alone. A location is only probed when a caller needs its state and the last answer is
older than half a minute; the probe of a share wakes it when it sits behind an automount
trigger. Every probe runs on a thread of its own, so a share whose server is gone never holds
up the other locations, the commands of this controller or the shutdown of the server.

A network share an admin adds is mounted by the Home Assistant Supervisor where the server
may manage its mounts, else by the server itself where it is allowed to mount. The share stays
with the backend that mounted it first, at the same path, and is mounted again on every start
of the server.
"""

from __future__ import annotations

import asyncio
import os
import stat
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope
from music_assistant_models.errors import (
    ActionUnavailable,
    InvalidDataError,
    MusicAssistantError,
    SetupFailedError,
)

from music_assistant.constants import (
    CONF_STORAGE_FOLDERS,
    CONF_STORAGE_SHARES,
    FILESYSTEM_PROVIDER_DOMAINS,
)
from music_assistant.controllers.storage.backends.base import (
    BackendUnavailable,
    ShareMounter,
    ShareState,
)
from music_assistant.controllers.storage.backends.local_mount import create_local_mounter
from music_assistant.controllers.storage.backends.mountinfo import (
    AUTOMOUNT_FSTYPE,
    MediaMount,
    find_share_mount,
    is_mounted,
    parse_mountinfo,
    parse_mountpoints,
    read_mountinfo,
)
from music_assistant.controllers.storage.backends.supervisor import create_supervisor_mounter
from music_assistant.controllers.storage.constants import (
    CONTAINER_MARKER_FILES,
    DIR_SIZE_MAX_AGE,
    DIR_SIZES_TASK_ID,
    MAX_LISTED_FOLDERS,
    PROBE_MAX_AGE,
    PROBE_TIMEOUT,
    RECONCILE_TASK_ID,
    REFRESH_INTERVAL,
    REFRESH_TASK_ID,
    SHARE_STATES_TIMEOUT,
    SHARES_DOCS_URL,
    SHARES_SETUP_TASK_ID,
)
from music_assistant.controllers.storage.helpers import is_within, share_key
from music_assistant.controllers.storage.models import (
    MountBackend,
    NetworkShareSpec,
    ShareType,
    StorageInfo,
    StorageKind,
    StorageLocation,
    StorageUsage,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_scope,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.util import get_folder_size, get_ip_from_host
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from music_assistant_models.config_entries import CoreConfig

    from music_assistant.helpers.json import SerializableType
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
        # the data and cache directory of the server, as given until setup resolves them
        self._server_folders = (
            os.path.normpath(mass.storage_path),
            os.path.normpath(mass.cache_path),
        )
        self._probes: dict[str, _ProbeState] = {}
        # every mountpoint the mount table showed since the start: an unmounted drive or share
        # leaves an empty folder behind, which must not pass for the storage itself
        self._seen_mountpoints: set[str] = set()
        # paths whose probe is being waited for; an answer that comes later rebuilds the list
        self._awaited_probes: set[str] = set()
        self._dir_sizes: dict[StorageUsage, float] = {}
        self._dir_sizes_requested: float | None = None
        # the mount backends this server can use, in the order of priority
        self._mounters: dict[MountBackend, ShareMounter] = {}
        # why a mount backend can not be used
        self._backend_problems: dict[MountBackend, str] = {}
        self._backends_lock = asyncio.Lock()
        # held while a network share is added, changed, mounted or removed
        self._shares_lock = asyncio.Lock()
        # why a network share could not be mounted the last time it was tried
        self._share_errors: dict[str, MusicAssistantError] = {}
        # shares whose mount was changed into another share outside Music Assistant
        self._changed_shares: set[str] = set()

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
        await self._resolve_server_folders()
        # the mount table only: a location is probed once a caller needs its state
        await self._periodic_refresh()
        self._request_dir_sizes()
        self.mass.create_task(self._setup_network_shares(), task_id=SHARES_SETUP_TASK_ID)

    async def close(self) -> None:
        """Handle logic on server stop."""
        self.mass.cancel_timer(REFRESH_TASK_ID)
        self.mass.cancel_task(REFRESH_TASK_ID)
        self.mass.cancel_task(DIR_SIZES_TASK_ID)
        # the network shares stay mounted: the Supervisor keeps its mounts anyway, and a share
        # this server mounted is mounted again on the next start
        self.mass.cancel_task(SHARES_SETUP_TASK_ID)
        self.mass.cancel_task(RECONCILE_TASK_ID)

    @api_command("storage/info", required_scope=READ_SCOPES)
    async def get_info(self) -> StorageInfo:
        """
        Return the storage locations the caller may see and what can be added.

        Probes the locations whose state is outdated first, which takes at most about 10 seconds.
        """
        manages_all_sources = _caller_manages_all_sources()
        if manages_all_sources:
            # the used space is only shown on the data and cache rows these callers see
            self._request_dir_sizes()
            # a share may have been changed in Home Assistant since it was last looked at
            await self._refresh_share_states()
        # a drive or share mounted since the last refresh shows up right away
        await self.refresh()
        await self._probe_outdated(loc.path for loc in self.get_locations(manages_all_sources))
        # only a caller that can add a share makes the server look for a mount backend again
        mounter = await self._get_mounter() if manages_all_sources else self._find_mounter()
        share_versions = mounter.supported_versions if mounter is not None else {}
        locations = self.get_locations(manages_all_sources)
        if not manages_all_sources:
            # a folder picker needs no connection details
            locations = [_without_share_details(location) for location in locations]
        return StorageInfo(
            locations=locations,
            can_mount_shares=mounter is not None,
            mount_backend=mounter.backend if mounter is not None else None,
            supported_share_types=list(share_versions),
            supported_share_versions={
                share_type: list(versions) for share_type, versions in share_versions.items()
            },
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

        :param path: Absolute path of an existing folder that is no storage location yet, also
            once its symlinks are resolved. It is stored, and returned, with its symlinks
            resolved.
        """
        if not self.can_add_local_folder:
            msg = "A folder can not be added when the server runs in a container"
            raise self._error(ActionUnavailable, msg, "local_folder_not_allowed")
        path = os.path.normpath(path)
        if not Path(path).is_absolute():
            raise self._error(
                InvalidDataError, f"Not an absolute path: {path}", "folder_path_not_absolute"
            )
        self._check_new_folder(path)
        # the probe also resolves the symlinks of the folder
        if (result := (await self._wait_for_probes([path]))[path]) is None:
            raise self._folder_unreadable(path)
        if not result.is_dir:
            raise self._folder_not_found(path)
        # the resolved path, which is what a folder picked in the location is checked against
        if result.real_path is not None and result.real_path != path:
            path = result.real_path
            self._check_new_folder(path)
            # the location is listed with the answer for its own path
            await self._wait_for_probes([path])
        self.mass.config.set(
            CONF_STORAGE_FOLDERS, [*self._get_registered_folders(), path], immediate=True
        )
        await self.refresh()
        return next(loc for loc in self._locations if loc.path == path)

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
            raise self._error(InvalidDataError, msg, "folder_not_registered")
        if (source := self._get_source_using(path)) is not None:
            msg = f"{source.name} uses {path}"
            raise self._error(ActionUnavailable, msg, "location_in_use", source.name)
        self.mass.config.set(
            CONF_STORAGE_FOLDERS, [folder for folder in folders if folder != path], immediate=True
        )
        await self.refresh()

    @api_command("storage/network_shares/add", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def add_network_share(
        self,
        share_type: ShareType,
        server: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        version: str | None = None,
        read_only: bool = False,
    ) -> StorageLocation:
        """
        Mount a network share as a media location.

        Refused when the share is a storage location already, also when it was added in Home
        Assistant. Nothing is kept when the share does not mount.

        Under a Supervisor only the Supervisor mounts a share.

        :param share_type: The protocol of the share.
        :param server: The hostname or IP address of the server.
        :param share: The share name of a cifs share, the absolute export path of an nfs share.
        :param username: The user to log in as (cifs only), None for guest access.
        :param password: The password of the user, required with a user.
        :param version: One of the supported protocol versions, None to negotiate it.
        :param read_only: Whether to mount the share read-only.
        """
        if (mounter := await self._get_mounter()) is None:
            if self.mass.running_as_hass_addon:
                msg = "The Supervisor does not mount network shares right now"
                raise self._error(ActionUnavailable, msg, "supervisor_mounts_unavailable")
            raise self._error(
                ActionUnavailable,
                "This server can not mount network shares",
                "no_mount_backend",
                SHARES_DOCS_URL,
            )
        spec = await self._check_share(
            mounter,
            NetworkShareSpec(
                name="",
                share_type=share_type,
                server=server,
                share=share,
                backend=mounter.backend,
                path="",
                username=username,
                version=version,
                read_only=read_only,
            ),
        )
        self._check_credentials(spec, bool(password))
        if spec.username is not None and password:
            spec.password = self.mass.config.encrypt_string(password)
        async with self._shares_lock:
            shares = self._get_shares()
            await self._check_not_added(mounter, spec, shares)
            spec = await mounter.assign_name(spec, shares.keys())
            await mounter.add(spec, self._get_password(spec))
            if not await self._is_share_mounted(spec):
                await self._undo_mount(mounter, spec)
                raise self._share_not_mounted(spec)
            self._share_errors.pop(spec.name, None)
            self.mass.config.set(
                f"{CONF_STORAGE_SHARES}/{spec.name}", spec.to_dict(), immediate=True
            )
        await self.refresh()
        return self._get_share_location(spec)

    @api_command("storage/network_shares/update", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def update_network_share(
        self,
        name: str,
        server: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        version: str | None = None,
        read_only: bool = False,
    ) -> StorageLocation:
        """
        Replace the settings of a network share and mount it with them.

        The share keeps its previous settings when it does not mount with the new ones.

        :param name: The name of the share.
        :param server: The hostname or IP address of the server.
        :param share: The share name of a cifs share, the absolute export path of an nfs share.
        :param username: The user to log in as (cifs only), None for guest access.
        :param password: The password of the user, None to keep the stored one. Required with a
            user when none is stored, dropped when there is no user.
        :param version: One of the supported protocol versions, None to negotiate it.
        :param read_only: Whether to mount the share read-only.
        """
        async with self._shares_lock:
            previous = self._get_share(name)
            mounter = await self._get_backend_mounter(previous)
            spec = await self._check_share(
                mounter,
                replace(
                    previous,
                    server=server,
                    share=share,
                    username=username,
                    version=version,
                    read_only=read_only,
                ),
            )
            self._check_credentials(spec, bool(password or previous.password))
            if spec.username is None:
                spec.password = None
            elif password:
                spec.password = self.mass.config.encrypt_string(password)
            await self._check_not_added(mounter, spec, self._get_shares())
            await self._check_not_changed(mounter, previous)
            try:
                await mounter.update(spec, self._get_password(spec))
            except Exception:
                # the backend still has the previous settings, it only has to mount them again
                await self._restore_share(mounter.reload, previous)
                await self.refresh()
                raise
            if not await self._is_share_mounted(spec):
                await self._restore_share(mounter.update, previous)
                await self.refresh()
                raise self._share_not_mounted(spec)
            self._share_errors.pop(name, None)
            self.mass.config.set(f"{CONF_STORAGE_SHARES}/{name}", spec.to_dict(), immediate=True)
        await self.refresh()
        return self._get_share_location(spec)

    @api_command("storage/network_shares/remove", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def remove_network_share(self, name: str) -> None:
        """
        Unmount a network share and remove it from the media locations.

        Refused while a music source uses the share, and when its mount can not be removed. A
        mount that was changed into another share in Home Assistant is the user's: it stays, and
        only the share is forgotten here.

        :param name: The name of the share.
        """
        async with self._shares_lock:
            spec = self._get_share(name)
            if (source := self._get_source_using(spec.path)) is not None:
                msg = f"{source.name} uses {spec.path}"
                raise self._error(ActionUnavailable, msg, "location_in_use", source.name)
            if (mounter := await self._get_mounter(spec.backend)) is None:
                if spec.backend == MountBackend.SUPERVISOR and self.mass.running_as_hass_addon:
                    # the Supervisor may only be busy: its mount stays until it can be removed
                    raise self._backend_unavailable(spec)
                self.logger.warning(
                    "Forgetting network share %s: %s has no mount of it on this installation",
                    name,
                    spec.backend,
                )
            elif (await mounter.get_states([spec]))[spec.name] == ShareState.CHANGED:
                # the mount is another share now, the user's: it stays
                self.logger.info(
                    "Forgetting network share %s, its mount was changed into another share", name
                )
            else:
                await mounter.remove(spec)
            self._share_errors.pop(name, None)
            self._changed_shares.discard(name)
            self.mass.config.remove(f"{CONF_STORAGE_SHARES}/{name}")
            self.mass.config.save(immediate=True)
        await self.refresh()

    @api_command("storage/network_shares/reload", required_scope=Scope.CONFIG_PROVIDERS_WRITE)
    async def reload_network_share(self, name: str) -> StorageLocation:
        """
        Mount a network share again, for example after its server was offline.

        :param name: The name of the share.
        """
        async with self._shares_lock:
            spec = self._get_share(name)
            mounter = await self._get_backend_mounter(spec)
            await self._check_not_changed(mounter, spec)
            try:
                await mounter.reload(spec, self._get_password(spec))
                if not await self._is_share_mounted(spec):
                    raise self._share_not_mounted(spec)
            except Exception as err:
                self._share_errors[name] = _as_share_error(err)
                await self.refresh()
                raise
            self._share_errors.pop(name, None)
        await self.refresh()
        return self._get_share_location(spec)

    def get_locations(self, manages_all_sources: bool = True) -> list[StorageLocation]:
        """
        Return the storage locations a caller may see.

        :param manages_all_sources: Whether the caller manages every music source. Any other
            caller only sees the media locations that were deliberately made available: all of
            them inside a container or under a Supervisor, the managed ones on a host.
        """
        return [loc for loc in self._locations if self._is_visible(loc, manages_all_sources)]

    def get_location_for_path(self, path: str) -> StorageLocation | None:
        """
        Return the most specific storage location that contains a path.

        :param path: An absolute path.
        """
        return max(
            (loc for loc in self._locations if is_within(path, loc.path)),
            key=lambda loc: len(loc.path),
            default=None,
        )

    def can_hold_music_source(self, path: str, manages_all_sources: bool) -> bool:
        """
        Return whether a music source may read its files from a path.

        The most specific location that contains the path decides: it must be a media location
        the caller may see. So a path is refused inside the data or cache folder of the server,
        and inside a location the caller may not see, also when those lie inside a location the
        caller may see. The path is taken as given: its symlinks are not resolved.

        :param path: An absolute path.
        :param manages_all_sources: Whether the caller manages every music source.
        """
        location = self.get_location_for_path(os.path.normpath(path))
        return (
            location is not None
            and location.usage == StorageUsage.MEDIA
            and self._is_visible(location, manages_all_sources)
        )

    async def is_available(self, path: str) -> bool:
        """
        Return whether a folder can be used right now.

        A folder on a mount is only available while that mount is there, so the empty directory
        an unmounted drive or share leaves behind does not count, also once the mount is no
        longer listed. Probes the location first when its state is outdated, which takes at
        most about 10 seconds.

        :param path: An absolute path.
        """
        if (location := self.get_location_for_path(path)) is not None:
            await self._probe_outdated([location.path])
            location = self.get_location_for_path(path)
        if location is not None and not location.available:
            # nothing more to look at: it may be a share whose server is gone
            return False
        mountpoints = set(self._seen_mountpoints)
        if location is not None and location.mountpoint is not None:
            # a managed network share needs its mount also when it did not mount since the start
            mountpoints.add(location.mountpoint)
        mountpoint = max(
            (mountpoint for mountpoint in mountpoints if is_within(path, mountpoint)),
            key=len,
            default=None,
        )
        return await asyncio.to_thread(_is_available, path, mountpoint)

    async def list_folders(self, path: str, manages_all_sources: bool = True) -> list[str]:
        """
        Return the names of the subfolders of a folder in a media location, sorted.

        Hidden folders and symlinks are left out and at most 500 names are returned. Probes the
        location first when its state is outdated, which takes at most about 10 seconds.

        :param path: A path a music source may be put on for the caller: a media location the
            caller may see or a folder inside one, never inside the data or cache folder of the
            server.
        :param manages_all_sources: Whether the caller manages every music source.
        """
        path = os.path.normpath(path)
        # checked before the probe too, so a caller only makes the server probe what it may use
        if not self.can_hold_music_source(path, manages_all_sources):
            raise self._path_not_allowed(path)
        if (location := self.get_location_for_path(path)) is not None:
            await self._probe_outdated([location.path])
        if (
            not self.can_hold_music_source(path, manages_all_sources)
            or (location := self.get_location_for_path(path)) is None
        ):
            raise self._path_not_allowed(path)
        if not location.available:
            raise self._folder_unreadable(path)
        # the path must also stay inside a location once its symlinks are resolved, and follow
        # the same rule there
        real_path = await asyncio.to_thread(
            _resolve_within, path, self._visible_roots(path, manages_all_sources)
        )
        if real_path is None or not self.can_hold_music_source(real_path, manages_all_sources):
            raise self._path_not_allowed(path)
        try:
            names = await asyncio.to_thread(_list_subfolders, real_path)
        except FileNotFoundError, NotADirectoryError:
            raise self._folder_not_found(path) from None
        except OSError as err:
            # e.g. a folder without read permission
            raise self._folder_unreadable(path) from err
        hidden = {
            loc.path
            for loc in self._locations
            if loc.usage == StorageUsage.MEDIA and not self._is_visible(loc, manages_all_sources)
        }
        # a media location the caller may not see is not shown by its name either; the server's
        # own folders are, they only can not be browsed into
        return [name for name in names if os.path.join(real_path, name) not in hidden]

    async def refresh(self) -> None:
        """Rebuild the list of storage locations from the mount table, touching no location."""
        table = await asyncio.to_thread(read_mountinfo)
        self._seen_mountpoints.update(parse_mountpoints(table))
        data_path, cache_path = self._server_folders
        mounts = {mount.mountpoint: mount for mount in self._parse_mounts(table)}
        media: dict[str, StorageLocation] = {}
        for mount in mounts.values():
            # a mount in the table counts as usable until a probe says otherwise
            answer = self._get_answer(mount.mountpoint, _UNPROBED_FOLDER)
            if answer is not None and not answer.is_dir:
                # a file bound into a container
                continue
            name = "Media folder" if mount.kind == StorageKind.BUILTIN_MEDIA else None
            media[mount.mountpoint] = _build_location(
                mount.mountpoint, name, StorageUsage.MEDIA, mount.kind, answer, mount=mount
            )
        # a registered folder replaces a discovered location on the same path, mount included
        for folder in self._get_registered_folders():
            folder_mount = mounts.get(folder)
            media[folder] = _build_location(
                folder,
                None,
                StorageUsage.MEDIA,
                StorageKind.MANUAL,
                # a plain folder may have gone, it counts as usable once a probe found it
                self._get_answer(folder, _UNPROBED_FOLDER if folder_mount is not None else None),
                mount=folder_mount,
                managed=True,
            )
        # a managed network share replaces the location discovered on its path the same way
        for spec in self._get_shares().values():
            media[spec.path] = self._build_share_location(spec, table)
        server_kind = StorageKind.CONTAINER_VOLUME if self._in_container else StorageKind.LOCAL_DISK
        self._locations = [
            *sorted(media.values(), key=lambda loc: loc.path.casefold()),
            *(
                _build_location(
                    path,
                    name,
                    usage,
                    server_kind,
                    # the server runs from these
                    self._get_answer(path, _UNPROBED_FOLDER),
                    used_space_gb=self._dir_sizes.get(usage),
                )
                for path, name, usage in (
                    (data_path, "Data", StorageUsage.DATA),
                    (cache_path, "Cache", StorageUsage.CACHE),
                )
            ),
        ]

    async def reconcile(self) -> None:
        """
        Mount the managed network shares that are not mounted, each through its own backend.

        That includes a mount the backend has but reports as not working. A share that can not
        be mounted stays a location that is not available and says why, until a reload or the
        next start of the server mounts it. Never raises.
        """
        try:
            async with self._shares_lock:
                shares = list(self._get_shares().values())
                for backend in MountBackend:
                    if specs := [spec for spec in shares if spec.backend == backend]:
                        await self._reconcile_backend(backend, specs)
            await self.refresh()
        except Exception:
            self.logger.exception("Failed to mount the network shares")

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return how this server mounts network shares and the state of each location."""
        mounter = self._find_mounter()
        # kinds and states only: a name, server or path of a location may identify the user
        return {
            "mount_backend": mounter.backend.value if mounter is not None else None,
            "mount_backends": {
                backend.value: "available"
                if backend in self._mounters
                else self._backend_problems.get(backend, "not probed")
                for backend in MountBackend
            },
            "locations": [
                {
                    "kind": location.kind.value,
                    "usage": location.usage.value,
                    "fstype": location.fstype,
                    "available": location.available,
                    "managed": location.managed,
                    "backend": location.backend.value if location.backend is not None else None,
                    "error": location.error_key,
                }
                for location in self._locations
            ],
        }

    async def _periodic_refresh(self) -> None:
        """Refresh the storage locations and schedule the next refresh."""
        try:
            await self.refresh()
        except Exception:
            self.logger.exception("Failed to refresh the storage locations")
        self.mass.call_later(REFRESH_INTERVAL, self._periodic_refresh, task_id=REFRESH_TASK_ID)

    async def _resolve_server_folders(self) -> None:
        """Resolve the data and cache directory the server was started with."""
        # the server may have been started with a relative path or one through a symlink,
        # while the mount table and the resolved paths of folders hold the real directories
        self._server_folders = await asyncio.to_thread(
            _real_paths, self.mass.storage_path, self.mass.cache_path
        )

    def _check_new_folder(self, path: str) -> None:
        """
        Raise when a path may not be registered as a folder.

        :param path: A normalized absolute path.
        """
        if not path.strip("/"):
            raise self._error(
                InvalidDataError, "The root folder can not be added", "folder_is_root"
            )
        if path in self._server_folders:
            msg = f"{path} is a folder of the server itself"
            raise self._error(InvalidDataError, msg, "folder_is_server_folder")
        if any(loc.path == path for loc in self._locations):
            msg = f"{path} already is a storage location"
            raise self._error(InvalidDataError, msg, "folder_already_location")

    def _visible_roots(self, path: str, manages_all_sources: bool) -> list[str]:
        """
        Return the media locations a caller may see that contain a path.

        :param path: A normalized path.
        :param manages_all_sources: Whether the caller manages every music source.
        """
        return [
            loc.path
            for loc in self.get_locations(manages_all_sources)
            if loc.usage == StorageUsage.MEDIA and is_within(path, loc.path)
        ]

    def _is_visible(self, location: StorageLocation, manages_all_sources: bool) -> bool:
        """
        Return whether a caller may see a storage location.

        :param location: The location.
        :param manages_all_sources: Whether the caller manages every music source.
        """
        return manages_all_sources or (
            location.usage == StorageUsage.MEDIA and (self._in_container or location.managed)
        )

    def _parse_mounts(self, table: str) -> list[MediaMount]:
        """Return the media mounts in a mount table of the server process."""
        return parse_mountinfo(
            table,
            excluded_paths=self._server_folders,
            in_container=self._in_container,
            supervisor=self.mass.running_as_hass_addon,
        )

    async def _probe_outdated(self, paths: Iterable[str]) -> None:
        """
        Probe the paths whose last answer is missing or outdated, and rebuild the list.

        A path whose probe already did not answer in time is not waited for again: it counts as
        not answering until that probe answers.

        :param paths: The paths whose state a caller needs.
        """
        now = time.monotonic()
        outdated = [
            path
            for path in paths
            if (state := self._probes.get(path)) is None
            or (not state.overdue and not state.is_fresh(now))
        ]
        if outdated:
            await self._wait_for_probes(outdated)
            # read the mount table again: a probe wakes an automount trigger
            await self.refresh()

    async def _wait_for_probes(
        self, paths: Iterable[str], fresh: bool = False
    ) -> dict[str, _ProbeResult | None]:
        """
        Probe paths and wait for their answers, at most PROBE_TIMEOUT seconds.

        Returns the answer for each path, None for a path whose probe did not answer in time.

        :param paths: The paths to probe.
        :param fresh: Whether to start new probes even where one is in flight.
        """
        probes = {path: self._probe(path, fresh) for path in paths}
        self._awaited_probes.update(probes)
        try:
            await asyncio.wait(probes.values(), timeout=PROBE_TIMEOUT)
        finally:
            self._awaited_probes.difference_update(probes)
        answers: dict[str, _ProbeResult | None] = {}
        for path, probe in probes.items():
            if probe.done():
                answers[path] = probe.result()
                continue
            answers[path] = None
            # a probe that a fresh one replaced says nothing about the path any more
            if self._probes[path].probe is probe:
                self._probes[path].overdue = True
        return answers

    def _probe(self, path: str, fresh: bool = False) -> asyncio.Future[_ProbeResult | None]:
        """
        Return the probe of a path, starting a new one unless one is still in flight.

        :param path: The path to probe.
        :param fresh: Whether to start a new probe even when one is in flight, for a path that
            was just mounted again: the probe in flight may hang on what was mounted before, and
            its answer is dropped.
        """
        state = self._probes.setdefault(path, _ProbeState())
        if state.probe is not None and not fresh:
            return state.probe
        state.overdue = False
        loop = self.mass.loop
        probe = state.probe = loop.create_future()
        probe.add_done_callback(lambda _probe: self._on_probe_answered(path, _probe))

        def _run() -> None:
            result = _probe_path(path)
            # the server may have stopped while the probe was blocked
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(_resolve_probe, probe, result)

        # a thread of its own: a blocked probe must neither take a worker of the default
        # executor nor hold up the shutdown of the server
        threading.Thread(target=_run, name="storage_probe", daemon=True).start()
        return probe

    def _on_probe_answered(self, path: str, probe: asyncio.Future[_ProbeResult | None]) -> None:
        """
        Keep the answer of a probe, and show it when it came after its caller stopped waiting.

        :param path: The probed path.
        :param probe: The probe that answered.
        """
        state = self._probes[path]
        if state.probe is not probe:
            # replaced by a fresh probe: this answer is about what was mounted before
            return
        state.answer = probe.result()
        state.answered_at = time.monotonic()
        state.probe = None
        state.overdue = False
        if path not in self._awaited_probes and not self.mass.closing:
            self.mass.create_task(self.refresh())

    def _get_answer(self, path: str, unprobed: _ProbeResult | None) -> _ProbeResult | None:
        """
        Return what describes a path now: its last answer, None when it does not answer.

        The last answer stays while a new probe is in flight, until that probe is overdue.

        :param path: The probed path.
        :param unprobed: What to assume for a path that was never probed.
        """
        if (state := self._probes.get(path)) is None:
            return unprobed
        if state.overdue:
            return None
        return state.answer if state.answered_at is not None else unprobed

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
        data_path, cache_path = self._server_folders
        # the default cache directory lies inside the data directory, and has a row of its own
        exclude = (cache_path,) if is_within(cache_path, data_path) else ()
        self._dir_sizes = {
            StorageUsage.DATA: round(await get_folder_size(data_path, exclude), 2),
            StorageUsage.CACHE: round(await get_folder_size(cache_path), 2),
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
            if isinstance(base_path, str) and is_within(base_path, path):
                return provider
        return None

    async def _setup_network_shares(self) -> None:
        """Find the mount backends this server can use and mount the managed network shares."""
        await self._probe_backends()
        await self.reconcile()

    async def _probe_backends(self) -> None:
        """Find the mount backends this server can use."""
        async with self._backends_lock:
            mounters: dict[MountBackend, ShareMounter] = {}
            problems: dict[MountBackend, str] = {}
            candidates: list[tuple[MountBackend, Callable[[], Awaitable[ShareMounter]]]] = [
                (MountBackend.SUPERVISOR, partial(create_supervisor_mounter, self.mass))
            ]
            if self.mass.running_as_hass_addon:
                # the app does not keep its mount privileges: a share it mounted itself could
                # never be mounted again
                problems[MountBackend.LOCAL_MOUNT] = "only the Supervisor mounts under a Supervisor"
            else:
                candidates.append(
                    (MountBackend.LOCAL_MOUNT, partial(create_local_mounter, self.logger))
                )
            for backend, create_mounter in candidates:
                try:
                    mounters[backend] = await create_mounter()
                except BackendUnavailable as err:
                    problems[backend] = str(err)
            self._mounters, self._backend_problems = mounters, problems
        self.logger.debug(
            "Network shares can be mounted by: %s; not by: %s",
            ", ".join(mounters) or "nothing",
            ", ".join(f"{backend} ({problem})" for backend, problem in problems.items()),
        )

    async def _get_mounter(self, backend: MountBackend | None = None) -> ShareMounter | None:
        """
        Return the mounter of a backend, probing the backends again when it is not available.

        :param backend: The backend, None for the one that mounts a new share.
        """
        if (mounter := self._find_mounter(backend)) is not None:
            return mounter
        available = set(self._mounters)
        await self._probe_backends()
        if set(self._mounters) - available:
            # the shares of a backend that became available can be mounted now
            self.mass.create_task(self.reconcile(), task_id=RECONCILE_TASK_ID)
        return self._find_mounter(backend)

    async def _get_backend_mounter(self, spec: NetworkShareSpec) -> ShareMounter:
        """
        Return the mounter of the backend that mounted a network share.

        :param spec: The share.
        """
        if (mounter := await self._get_mounter(spec.backend)) is None:
            raise self._backend_unavailable(spec)
        return mounter

    def _find_mounter(self, backend: MountBackend | None = None) -> ShareMounter | None:
        """
        Return the mounter of a backend when it is available.

        :param backend: The backend, None for the one that mounts a new share.
        """
        if backend is None:
            return next(iter(self._mounters.values()), None)
        return self._mounters.get(backend)

    async def _reconcile_backend(
        self, backend: MountBackend, specs: list[NetworkShareSpec]
    ) -> None:
        """
        Mount the network shares of one backend that are not mounted.

        :param backend: The backend.
        :param specs: The shares of the backend.
        """
        if (mounter := self._mounters.get(backend)) is None:
            for spec in specs:
                self._share_errors[spec.name] = self._backend_unavailable(spec)
            return
        try:
            states = await mounter.get_states(specs)
        except Exception as err:
            self.logger.warning(
                "Unable to check the network shares mounted by %s: %s", backend, err
            )
            for spec in specs:
                self._share_errors[spec.name] = _as_share_error(err)
            return
        for spec in specs:
            state = states.get(spec.name, ShareState.MISSING)
            if state == ShareState.CHANGED:
                self._changed_shares.add(spec.name)
                continue
            self._changed_shares.discard(spec.name)
            if state == ShareState.PRESENT:
                self._share_errors.pop(spec.name, None)
                continue
            operation = mounter.reload if state == ShareState.FAILED else mounter.add
            try:
                await operation(spec, self._get_password(spec))
            except Exception as err:
                self.logger.warning("Unable to mount network share %s: %s", spec.name, err)
                self._share_errors[spec.name] = _as_share_error(err)
            else:
                self._share_errors.pop(spec.name, None)

    async def _refresh_share_states(self) -> None:
        """Note which managed shares had their mount changed into another share, changing nothing."""
        if self._shares_lock.locked():
            # a share command is at work: the states are known again when it is done
            return
        async with self._shares_lock:
            shares = list(self._get_shares().values())
            for backend, mounter in self._mounters.items():
                if not (specs := [spec for spec in shares if spec.backend == backend]):
                    continue
                try:
                    async with asyncio.timeout(SHARE_STATES_TIMEOUT):
                        states = await mounter.get_states(specs)
                except Exception as err:
                    self.logger.debug(
                        "Unable to check the network shares mounted by %s: %s", backend, err
                    )
                    continue
                for spec in specs:
                    if states.get(spec.name) == ShareState.CHANGED:
                        self._changed_shares.add(spec.name)
                    else:
                        self._changed_shares.discard(spec.name)

    async def _check_share(self, mounter: ShareMounter, spec: NetworkShareSpec) -> NetworkShareSpec:
        """
        Return the settings of a network share cleaned up, when a backend can mount them.

        :param mounter: The mounter of the backend.
        :param spec: The share with the settings as entered.
        """
        is_cifs = spec.share_type == ShareType.CIFS
        share = spec.share.strip()
        spec = replace(
            spec,
            server=spec.server.strip(),
            # an export path compares the way the Supervisor stores it: /music/ is /music
            share=share if is_cifs else str(PurePosixPath(share)),
            username=((spec.username or "").strip() or None) if is_cifs else None,
            version=spec.version or None,
        )
        if (versions := mounter.supported_versions.get(spec.share_type)) is None:
            msg = f"Network shares of type {spec.share_type} can not be mounted"
            raise self._error(InvalidDataError, msg, "share_type_not_supported", spec.share_type)
        if spec.version is not None and spec.version not in versions:
            msg = f"Version {spec.version} of {spec.share_type} is not supported"
            raise self._error(InvalidDataError, msg, "share_version_not_supported", spec.version)
        # a comma would end up in the options of the mount command
        if is_cifs and (not spec.share or any(char in spec.share for char in "/\\,")):
            raise self._error(InvalidDataError, "Invalid share name", "share_name_invalid")
        if spec.username is not None and "," in spec.username:
            raise self._error(InvalidDataError, "Invalid user name", "share_username_invalid")
        if not is_cifs and not (spec.share.startswith("/") and is_safe_path(spec.share)):
            raise self._error(InvalidDataError, "Invalid export path", "export_path_invalid")
        if not spec.server or not await get_ip_from_host(spec.server):
            msg = f"Unable to resolve {spec.server}, make sure the address is resolvable."
            raise self._error(InvalidDataError, msg, "host_unresolvable", spec.server)
        return spec

    async def _check_not_added(
        self, mounter: ShareMounter, spec: NetworkShareSpec, shares: dict[str, NetworkShareSpec]
    ) -> None:
        """
        Raise when a network share is a storage location already, other than the share itself.

        :param mounter: The mounter of the backend of the share.
        :param spec: The share.
        :param shares: The managed shares, by name.
        """
        key = share_key(spec.share_type, spec.server, spec.share)
        if any(
            share_key(other.share_type, other.server, other.share) == key
            for name, other in shares.items()
            if name != spec.name
        ):
            msg = f"{spec.share} on {spec.server} already is a storage location"
            raise self._error(InvalidDataError, msg, "share_already_added")
        # a share mounted without Music Assistant, e.g. in Home Assistant, is a location as it is
        path = await mounter.find_mount(spec.share_type, spec.server, spec.share)
        if path is not None and path != spec.path:
            msg = f"{spec.share} on {spec.server} is mounted at {path} already"
            raise self._error(InvalidDataError, msg, "share_mounted_already", path)

    def _check_credentials(self, spec: NetworkShareSpec, has_password: bool) -> None:
        """
        Raise when a user comes without a password.

        :param spec: The share.
        :param has_password: Whether there is a password for the user.
        """
        if spec.username is not None and not has_password:
            msg = f"No password for user {spec.username}"
            raise self._error(InvalidDataError, msg, "share_password_missing")

    async def _check_not_changed(self, mounter: ShareMounter, spec: NetworkShareSpec) -> None:
        """
        Raise when the mount of a share was changed into another share outside Music Assistant.

        Such a mount is left alone, and its location is not available.

        :param mounter: The mounter of the backend of the share.
        :param spec: The share as stored.
        """
        if (await mounter.get_states([spec]))[spec.name] != ShareState.CHANGED:
            self._changed_shares.discard(spec.name)
            return
        self._changed_shares.add(spec.name)
        await self.refresh()
        raise self._share_changed(spec)

    async def _is_share_mounted(self, spec: NetworkShareSpec) -> bool:
        """
        Return whether a network share is mounted, probing it, which wakes an automount trigger.

        :param spec: The share.
        """
        answer = (await self._wait_for_probes([spec.path], fresh=True))[spec.path]
        return (
            answer is not None
            and answer.is_dir
            and await asyncio.to_thread(_is_mountpoint, spec.path)
        )

    async def _undo_mount(self, mounter: ShareMounter, spec: NetworkShareSpec) -> None:
        """
        Remove the mount of a new network share that did not mount, as far as possible.

        :param mounter: The mounter of the backend of the share.
        :param spec: The share.
        """
        try:
            await mounter.remove(spec)
        except Exception as err:
            self.logger.warning(
                "Unable to remove the mount of network share %s: %s", spec.name, err
            )

    async def _restore_share(
        self,
        operation: Callable[[NetworkShareSpec, str | None], Awaitable[None]],
        spec: NetworkShareSpec,
    ) -> None:
        """
        Mount a network share with its previous settings again, as far as possible.

        The share only counts as restored once a fresh probe finds it mounted.

        :param operation: The operation of the backend that mounts the share again.
        :param spec: The share with its previous settings.
        """
        try:
            await operation(spec, self._get_password(spec))
            # the last probe of the path went to the settings that did not mount
            if not await self._is_share_mounted(spec):
                raise self._share_not_mounted(spec)
        except Exception as err:
            self.logger.warning(
                "Unable to mount network share %s with its previous settings: %s", spec.name, err
            )
            self._share_errors[spec.name] = _as_share_error(err)
        else:
            self._share_errors.pop(spec.name, None)

    def _get_shares(self) -> dict[str, NetworkShareSpec]:
        """Return the network shares Music Assistant manages, by name."""
        shares: dict[str, NetworkShareSpec] = {}
        for name, record in self.mass.config.get(CONF_STORAGE_SHARES, {}).items():
            try:
                shares[name] = NetworkShareSpec.from_dict(record)
            except LookupError, ValueError:
                self.logger.debug("Skipping the unreadable network share %s", name)
        return shares

    def _get_share(self, name: str) -> NetworkShareSpec:
        """
        Return a network share Music Assistant manages.

        :param name: The name of the share.
        """
        if (spec := self._get_shares().get(name)) is None:
            msg = f"There is no network share named {name}"
            raise self._error(InvalidDataError, msg, "share_not_found", name)
        return spec

    def _get_share_location(self, spec: NetworkShareSpec) -> StorageLocation:
        """
        Return the listed location of a managed network share.

        :param spec: The share.
        """
        return next(loc for loc in self._locations if loc.path == spec.path)

    def _get_password(self, spec: NetworkShareSpec) -> str | None:
        """
        Return the decrypted password of a network share.

        :param spec: The share.
        """
        return self.mass.config.decrypt_string(spec.password) if spec.password else None

    def _build_share_location(self, spec: NetworkShareSpec, table: str) -> StorageLocation:
        """
        Build the location of a managed network share from the mount on its path.

        :param spec: The share.
        :param table: The mount table of the server process, empty on a system without one.
        """
        share_mount = find_share_mount(table, spec.path)
        changed = spec.name in self._changed_shares
        # the folder an unmounted share leaves behind is not the share, and a mount changed into
        # another share is not this one; without a mount table (macOS) the probe decides
        answer = (
            None
            if changed or (share_mount is None and table)
            else self._get_answer(spec.path, _UNPROBED_FOLDER)
        )
        location = _build_location(
            spec.path,
            None,
            StorageUsage.MEDIA,
            StorageKind.NETWORK_SHARE,
            answer,
            mount=share_mount,
            managed=True,
        )
        error: MusicAssistantError | None = None
        if changed:
            error = self._share_changed(spec)
        elif not location.available:
            error = self._share_errors.get(spec.name) or self._error(
                ActionUnavailable,
                f"Network share {spec.name} is not available",
                "share_unavailable",
            )
        return replace(
            location,
            read_only=spec.read_only or location.read_only,
            backend=spec.backend,
            mountpoint=spec.path,
            share_name=spec.name,
            share_type=spec.share_type,
            server=spec.server,
            share=spec.share,
            username=spec.username,
            version=spec.version,
            error=str(error) if error is not None else None,
            error_key=error.translation_key if error is not None else None,
            error_args=[str(arg) for arg in error.translation_args] if error is not None else [],
        )

    def _share_not_mounted(self, spec: NetworkShareSpec) -> SetupFailedError:
        """
        Return the error for a network share that is not mounted after it was mounted.

        :param spec: The share.
        """
        msg = f"Network share {spec.name} did not mount"
        return self._error(SetupFailedError, msg, "share_not_mounted")

    def _share_changed(self, spec: NetworkShareSpec) -> ActionUnavailable:
        """
        Return the error for a network share whose mount was changed into another share.

        :param spec: The share.
        """
        msg = f"Network share {spec.name} was changed in Home Assistant"
        return self._error(ActionUnavailable, msg, "share_changed")

    def _backend_unavailable(self, spec: NetworkShareSpec) -> ActionUnavailable:
        """
        Return the error for a network share whose mount backend is not available.

        :param spec: The share.
        """
        msg = f"Network share {spec.name} can not be mounted: {spec.backend} is not available"
        return self._error(ActionUnavailable, msg, "mount_backend_unavailable")

    def _path_not_allowed(self, path: str) -> InvalidDataError:
        """
        Return the error for a path outside the locations a caller may use.

        :param path: The refused path.
        """
        return self._error(
            InvalidDataError, f"Not inside a storage location: {path}", "path_not_allowed"
        )

    def _folder_not_found(self, path: str) -> InvalidDataError:
        """
        Return the error for a folder that does not exist.

        :param path: The folder that does not exist.
        """
        return self._error(
            InvalidDataError, f"Folder does not exist: {path}", "folder_not_found", path
        )

    def _folder_unreadable(self, path: str) -> ActionUnavailable:
        """
        Return the error for a folder that can not be read right now.

        :param path: The folder that can not be read.
        """
        return self._error(ActionUnavailable, f"Can not read {path}", "folder_unreadable", path)

    def _error[ErrorT: (ActionUnavailable, InvalidDataError, SetupFailedError)](
        self, error: type[ErrorT], msg: str, translation_key: str, *args: str
    ) -> ErrorT:
        """
        Return an error of this controller with a translated message.

        :param error: The error class.
        :param msg: The English message, for the log and as a fallback.
        :param translation_key: The key of the translated message in the strings of this
            controller.
        :param args: The values for the placeholders of the translated message.
        """
        return error(
            msg,
            translation_key=translation_key,
            translation_owner=self.translation_owner,
            translation_args=list(args),
        )


@dataclass(frozen=True)
class _ProbeResult:
    """What a probe found on a path."""

    is_dir: bool
    free_space_gb: float | None = None
    total_space_gb: float | None = None
    # the path with its symlinks resolved
    real_path: str | None = None


@dataclass
class _ProbeState:
    """The probes of one path: the last answer and the probe in flight, at most one."""

    # None when the path could not be reached
    answer: _ProbeResult | None = None
    # monotonic time of the answer, None while the path was never probed
    answered_at: float | None = None
    probe: asyncio.Future[_ProbeResult | None] | None = field(default=None, repr=False)
    # the probe in flight did not answer within PROBE_TIMEOUT
    overdue: bool = False

    def is_fresh(self, now: float) -> bool:
        """
        Return whether the last answer is recent enough to go by.

        :param now: The current monotonic time.
        """
        return self.answered_at is not None and now - self.answered_at < PROBE_MAX_AGE


# what a mount or folder is taken for until a probe says otherwise: usable, space unknown
_UNPROBED_FOLDER = _ProbeResult(is_dir=True)


def _caller_manages_all_sources() -> bool:
    """Return whether the caller of an API command manages every music source."""
    user = get_current_user()
    # no user context means an internal (server-side) caller, which is trusted
    return user is None or has_scope(user, Scope.CONFIG_PROVIDERS_WRITE)


def _running_in_container() -> bool:
    """Return whether the server runs in a Docker or Podman container (blocking)."""
    return any(Path(marker).exists() for marker in CONTAINER_MARKER_FILES)


def _real_paths(data_path: str, cache_path: str) -> tuple[str, str]:
    """Return the data and cache directory made absolute, symlinks resolved (blocking)."""
    return os.path.realpath(data_path), os.path.realpath(cache_path)


def _probe_path(path: str) -> _ProbeResult | None:
    """Return what is on a path, None when it can not be reached (blocking, may block long)."""
    try:
        # statvfs first: unlike a plain stat it wakes an automount trigger on the path
        fs_stats = os.statvfs(path)
        is_dir = stat.S_ISDIR(Path(path).stat().st_mode)
    except FileNotFoundError, NotADirectoryError, ValueError:
        return _ProbeResult(is_dir=False)
    except OSError:
        return None
    return _ProbeResult(
        is_dir=is_dir,
        free_space_gb=round(fs_stats.f_bavail * fs_stats.f_frsize / BYTES_PER_GB, 2),
        total_space_gb=round(fs_stats.f_blocks * fs_stats.f_frsize / BYTES_PER_GB, 2),
        real_path=os.path.realpath(path),
    )


def _resolve_probe(probe: asyncio.Future[_ProbeResult | None], result: _ProbeResult | None) -> None:
    """Hand the answer of a probe to its future."""
    if not probe.done():
        probe.set_result(result)


def _build_location(
    path: str,
    name: str | None,
    usage: StorageUsage,
    kind: StorageKind,
    result: _ProbeResult | None,
    *,
    mount: MediaMount | None = None,
    managed: bool = False,
    used_space_gb: float | None = None,
) -> StorageLocation:
    """Build a storage location from the latest probe of its path and the mount on it."""
    # a share still behind its automount trigger did not mount
    available = (
        result is not None and result.is_dir and (mount is None or mount.fstype != AUTOMOUNT_FSTYPE)
    )
    return StorageLocation(
        path=path,
        name=name or Path(path).name or path,
        usage=usage,
        kind=kind,
        available=available,
        read_only=mount.read_only if mount is not None else False,
        managed=managed,
        fstype=mount.fstype if mount is not None else None,
        mountpoint=mount.mountpoint if mount is not None else None,
        free_space_gb=result.free_space_gb if available and result is not None else None,
        total_space_gb=result.total_space_gb if available and result is not None else None,
        used_space_gb=used_space_gb,
    )


def _is_available(path: str, mountpoint: str | None) -> bool:
    """Return whether a folder is there, and its mount when it has one (blocking)."""
    if mountpoint is not None and not _is_mountpoint(mountpoint):
        return False
    return Path(path).is_dir()


def _is_mountpoint(path: str) -> bool:
    """Return whether a filesystem is mounted on a path (blocking)."""
    return is_mounted(path, read_mountinfo())


def _without_share_details(location: StorageLocation) -> StorageLocation:
    """Return a location without the connection details of a managed network share."""
    if location.share_name is None:
        return location
    # the error of a mount can name the server and the export
    has_error = location.error is not None
    return replace(
        location,
        share_name=None,
        server=None,
        share=None,
        username=None,
        version=None,
        error="The network share is not available right now" if has_error else None,
        error_key="share_unavailable" if has_error else None,
        error_args=[],
    )


def _as_share_error(err: Exception) -> MusicAssistantError:
    """Return the error to show on a network share that could not be mounted."""
    if isinstance(err, MusicAssistantError):
        return err
    msg = str(err) or type(err).__name__
    return SetupFailedError(msg, translation_key="mount_failed", translation_args=[msg])


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
