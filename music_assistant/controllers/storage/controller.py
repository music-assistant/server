"""
Storage controller: the storage locations the server can see.

Locations come from the mount table of the server process (container volumes, network shares
and drives), from the folders an admin registered on this server, and from the server's own
data and cache directories. A caller that manages every music source sees all of them; any
other caller sees the media locations that were deliberately made available to the server.

The list follows the mount table without touching any location, so an idle network share is
left alone. A location is only probed when a caller needs its state and the last answer is
older than half a minute; the probe of a share wakes it when it sits behind an automount
trigger. Every probe runs on a thread of its own, so a share whose server is gone never holds
up the other locations, the commands of this controller or the shutdown of the server.
"""

from __future__ import annotations

import asyncio
import os
import stat
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.auth import Scope
from music_assistant_models.errors import ActionUnavailable, InvalidDataError

from music_assistant.constants import CONF_STORAGE_FOLDERS, FILESYSTEM_PROVIDER_DOMAINS
from music_assistant.controllers.storage.backends.mountinfo import (
    AUTOMOUNT_FSTYPE,
    MediaMount,
    parse_mountinfo,
    parse_mountpoints,
    read_mountinfo,
)
from music_assistant.controllers.storage.constants import (
    CONTAINER_MARKER_FILES,
    DIR_SIZE_MAX_AGE,
    DIR_SIZES_TASK_ID,
    MAX_LISTED_FOLDERS,
    PROBE_MAX_AGE,
    PROBE_TIMEOUT,
    REFRESH_INTERVAL,
    REFRESH_TASK_ID,
)
from music_assistant.controllers.storage.helpers import is_within
from music_assistant.controllers.storage.models import (
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
from music_assistant.helpers.util import get_folder_size
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from collections.abc import Iterable

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
        self._probes: dict[str, _ProbeState] = {}
        # every mountpoint the mount table showed since the start: an unmounted drive or share
        # leaves an empty folder behind, which must not pass for the storage itself
        self._seen_mountpoints: set[str] = set()
        # paths whose probe is being waited for; an answer that comes later rebuilds the list
        self._awaited_probes: set[str] = set()
        self._dir_sizes: dict[StorageUsage, float] = {}
        self._dir_sizes_requested: float | None = None

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
        # the mount table only: a location is probed once a caller needs its state
        await self._periodic_refresh()
        self._request_dir_sizes()

    async def close(self) -> None:
        """Handle logic on server stop."""
        self.mass.cancel_timer(REFRESH_TASK_ID)
        self.mass.cancel_task(REFRESH_TASK_ID)
        self.mass.cancel_task(DIR_SIZES_TASK_ID)

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
        # a drive or share mounted since the last refresh shows up right away
        await self.refresh()
        await self._probe_outdated(loc.path for loc in self.get_locations(manages_all_sources))
        return StorageInfo(
            locations=self.get_locations(manages_all_sources),
            can_mount_shares=False,
            mount_backend=None,
            supported_share_types=[],
            supported_share_versions={},
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
            once its symlinks are resolved. It is stored as given.
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
        server_paths = self._server_paths()
        # the probes resolve the symlinks, of the server's own folders as well
        answers = await self._wait_for_probes([path, *server_paths])
        if (result := answers[path]) is None:
            raise self._folder_unreadable(path)
        if not result.is_dir:
            raise self._folder_not_found(path)
        real_server_paths = [
            answer.real_path
            for server_path in server_paths
            if (answer := answers[server_path]) is not None and answer.real_path is not None
        ]
        self._check_new_folder(result.real_path or path, real_server_paths)
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

    def get_locations(self, manages_all_sources: bool = True) -> list[StorageLocation]:
        """
        Return the storage locations a caller may see.

        :param manages_all_sources: Whether the caller manages every music source. Any other
            caller only sees the media locations that were deliberately made available: all of
            them inside a container or under a Supervisor, the managed ones on a host.
        """
        if manages_all_sources:
            return list(self._locations)
        return [
            loc
            for loc in self._locations
            if loc.usage == StorageUsage.MEDIA and (self._in_container or loc.managed)
        ]

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

        That is a path inside a media location the caller may see, and not inside the data or
        cache folder of the server, even when that folder lies inside such a location. The path
        is taken as given: its symlinks are not resolved.

        :param path: An absolute path.
        :param manages_all_sources: Whether the caller manages every music source.
        """
        path = os.path.normpath(path)
        return bool(self._visible_roots(path, manages_all_sources)) and not self._is_server_path(
            path
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
        mountpoint = max(
            (mountpoint for mountpoint in self._seen_mountpoints if is_within(path, mountpoint)),
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
        # the path must also stay inside a location once its symlinks are resolved, and out of
        # the server's own folders
        real_path = await asyncio.to_thread(
            _resolve_within, path, self._visible_roots(path, manages_all_sources)
        )
        if real_path is None or self._is_server_path(real_path):
            raise self._path_not_allowed(path)
        try:
            return await asyncio.to_thread(_list_subfolders, real_path)
        except FileNotFoundError, NotADirectoryError:
            raise self._folder_not_found(path) from None
        except OSError as err:
            # e.g. a folder without read permission
            raise self._folder_unreadable(path) from err

    async def refresh(self) -> None:
        """Rebuild the list of storage locations from the mount table, touching no location."""
        table = await asyncio.to_thread(read_mountinfo)
        self._seen_mountpoints.update(parse_mountpoints(table))
        data_path, cache_path = self._server_paths()
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

    async def _periodic_refresh(self) -> None:
        """Refresh the storage locations and schedule the next refresh."""
        try:
            await self.refresh()
        except Exception:
            self.logger.exception("Failed to refresh the storage locations")
        self.mass.call_later(REFRESH_INTERVAL, self._periodic_refresh, task_id=REFRESH_TASK_ID)

    def _check_new_folder(self, path: str, real_server_paths: Iterable[str] = ()) -> None:
        """
        Raise when a path may not be registered as a folder.

        :param path: A normalized absolute path.
        :param real_server_paths: The server's own folders with their symlinks resolved.
        """
        if not path.strip("/"):
            raise self._error(
                InvalidDataError, "The root folder can not be added", "folder_is_root"
            )
        if path in {*self._server_paths(), *real_server_paths}:
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

    def _is_server_path(self, path: str) -> bool:
        """
        Return whether a path lies in the data or cache folder of the server.

        :param path: A normalized absolute path.
        """
        location = self.get_location_for_path(path)
        return location is not None and location.usage != StorageUsage.MEDIA

    def _parse_mounts(self, table: str) -> list[MediaMount]:
        """Return the media mounts in a mount table of the server process."""
        return parse_mountinfo(
            table,
            excluded_paths=self._server_paths(),
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

    async def _wait_for_probes(self, paths: Iterable[str]) -> dict[str, _ProbeResult | None]:
        """
        Probe paths and wait for their answers, at most PROBE_TIMEOUT seconds.

        Returns the answer for each path, None for a path whose probe did not answer in time.

        :param paths: The paths to probe.
        """
        probes = {path: self._probe(path) for path in paths}
        self._awaited_probes.update(probes)
        try:
            await asyncio.wait(probes.values(), timeout=PROBE_TIMEOUT)
        finally:
            self._awaited_probes.difference_update(probes)
        answers: dict[str, _ProbeResult | None] = {}
        for path, probe in probes.items():
            if probe.done():
                answers[path] = probe.result()
            else:
                self._probes[path].overdue = True
                answers[path] = None
        return answers

    def _probe(self, path: str) -> asyncio.Future[_ProbeResult | None]:
        """
        Return the probe of a path, starting a new one unless one is still in flight.

        :param path: The path to probe.
        """
        state = self._probes.setdefault(path, _ProbeState())
        if state.probe is not None:
            return state.probe
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
        data_path, cache_path = self._server_paths()
        # the default cache directory lies inside the data directory, and has a row of its own
        exclude = (cache_path,) if is_within(cache_path, data_path) else ()
        self._dir_sizes = {
            StorageUsage.DATA: round(await get_folder_size(data_path, exclude), 2),
            StorageUsage.CACHE: round(await get_folder_size(cache_path), 2),
        }
        for location in self._locations:
            if location.usage != StorageUsage.MEDIA:
                location.used_space_gb = self._dir_sizes.get(location.usage)

    def _server_paths(self) -> tuple[str, str]:
        """Return the data and the cache directory of the server."""
        return os.path.normpath(self.mass.storage_path), os.path.normpath(self.mass.cache_path)

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

    def _error[ErrorT: (ActionUnavailable, InvalidDataError)](
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
