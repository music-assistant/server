"""
Network shares mounted by the server itself, where nothing better can mount them.

On Linux this needs root with CAP_SYS_ADMIN and CAP_DAC_READ_SEARCH (which a container only has
when it was started with them) and the mount helper of the protocol; macOS mounts a CIFS share for any user and an
NFS export for root only. The shares are mounted below ``/tmp/music-assistant-mounts``: outside
the data directory, so no backup walks a NAS. As ``/tmp`` is open to every user of the server,
the mount root is a folder of this process that nobody else can change, and nothing is mounted on
a folder another user could have prepared.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final

from music_assistant_models.errors import SetupFailedError

from music_assistant.controllers.storage.backends.base import (
    BackendUnavailable,
    ShareMounter,
    ShareState,
)
from music_assistant.controllers.storage.backends.mountinfo import is_mounted, read_mountinfo
from music_assistant.controllers.storage.constants import SHARES_DOCS_URL, TRANSLATION_OWNER
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType
from music_assistant.helpers.mount import (
    build_cifs_mount_cmd,
    build_nfs_mount_cmd,
    classify_mount_error,
    is_mount_not_permitted,
    unmount,
)
from music_assistant.helpers.process import check_output

if TYPE_CHECKING:
    from logging import Logger

MOUNT_ROOT: Final[str] = "/tmp/music-assistant-mounts"  # noqa: S108
MOUNT_TIMEOUT: Final[int] = 30
LOCAL_VERSIONS: Final[dict[ShareType, tuple[str, ...]]] = {
    ShareType.CIFS: ("1.0", "2.0", "2.1", "3.0", "3.1.1"),
    ShareType.NFS: ("3", "4", "4.1", "4.2"),
}
MOUNT_HELPERS: Final[dict[ShareType, str]] = {
    ShareType.CIFS: "mount.cifs",
    ShareType.NFS: "mount.nfs",
}
CAP_DAC_READ_SEARCH: Final[int] = 2
CAP_SYS_ADMIN: Final[int] = 21
PROC_STATUS_PATH: Final[str] = "/proc/self/status"


class UnsafeFolderError(Exception):
    """A folder to mount on that another user could have prepared; the message is its path."""


def get_local_mount_support(
    system: str, euid: int | None, cap_eff: int | None, has_helper: Callable[[str], bool]
) -> tuple[dict[ShareType, list[str]], str | None]:
    """
    Return the share types this process can mount itself, and why when it can mount none.

    Each share type comes with the protocol versions a share can be pinned to.

    :param system: The operating system, as ``platform.system()`` names it.
    :param euid: The effective user id of the process, None on a system without user ids.
    :param cap_eff: The effective capability set of the process (Linux), None when unknown.
    :param has_helper: Returns whether a mount helper binary is installed.
    """
    if system == "Darwin":
        # mount_smbfs mounts for any user, mount_nfs only for root
        share_types = [ShareType.CIFS, ShareType.NFS] if euid == 0 else [ShareType.CIFS]
    elif system != "Linux":
        return {}, f"mounting is not supported on {system}"
    elif euid != 0:
        return {}, "not running as root"
    elif missing := [
        name
        for name, capability in (
            ("CAP_SYS_ADMIN", CAP_SYS_ADMIN),
            ("CAP_DAC_READ_SEARCH", CAP_DAC_READ_SEARCH),
        )
        if cap_eff is None or not cap_eff & (1 << capability)
    ]:
        return {}, f"no {' and no '.join(missing)} capability"
    else:
        share_types = [
            share_type for share_type in ShareType if has_helper(MOUNT_HELPERS[share_type])
        ]
        if not share_types:
            return {}, "no mount helpers installed (mount.cifs, mount.nfs)"
    return {share_type: list(LOCAL_VERSIONS[share_type]) for share_type in share_types}, None


async def create_local_mounter(logger: Logger) -> LocalMounter:
    """
    Return the mounter of this process, when it may mount network shares itself.

    :param logger: The logger to report on.
    :raises BackendUnavailable: When this process can mount no network share.
    """
    supported, reason = await asyncio.to_thread(_probe_local_mount_support)
    if reason is not None:
        raise BackendUnavailable(reason)
    return LocalMounter(supported, logger)


class LocalMounter(ShareMounter):
    """Mounts network shares with the mount tools of the system."""

    backend = MountBackend.LOCAL_MOUNT

    def __init__(self, supported_versions: dict[ShareType, list[str]], logger: Logger) -> None:
        """
        Initialize the mounter.

        :param supported_versions: The share types this process can mount, with their versions.
        :param logger: The logger to report on.
        """
        super().__init__(supported_versions)
        self.logger = logger

    def get_path(self, name: str) -> str:
        """
        Return where a share with this name is mounted.

        :param name: The name of the share.
        """
        return f"{MOUNT_ROOT}/{name}"

    async def add(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share that is not mounted.

        :param spec: The share.
        :param password: The password of the share, decrypted.
        """
        system = platform.system()
        env: dict[str, str] = {}
        if spec.share_type == ShareType.CIFS:
            mount_cmd, env = build_cifs_mount_cmd(
                system,
                spec.server,
                spec.share,
                spec.path,
                username=spec.username,
                password=password,
                version=spec.version,
                read_only=spec.read_only,
            )
        else:
            mount_cmd = build_nfs_mount_cmd(
                system,
                spec.server,
                spec.share,
                spec.path,
                version=spec.version,
                read_only=spec.read_only,
            )
        self.logger.debug("Mounting network share %s on %s", spec.name, spec.path)
        try:
            await asyncio.to_thread(_prepare_mountpoint, MOUNT_ROOT, spec.path)
            returncode, output = await check_output(*mount_cmd, env=env, timeout=MOUNT_TIMEOUT)
        except UnsafeFolderError as err:
            raise _unsafe_folder(spec, err) from err
        except TimeoutError as err:
            # the mount may have completed just before the mount tool was stopped
            with suppress(SetupFailedError):
                await unmount(spec.path, self.logger, _is_mounted)
            msg = f"Mounting {spec.name} did not finish within {MOUNT_TIMEOUT} seconds"
            raise SetupFailedError(
                msg, translation_key="share_no_answer", translation_owner=TRANSLATION_OWNER
            ) from err
        except OSError as err:
            msg = f"Unable to mount {spec.name}: {err}"
            raise SetupFailedError(
                msg, translation_key="mount_failed", translation_args=[str(err)]
            ) from err
        if returncode == 0:
            return
        text = output.decode(errors="replace").strip()
        if is_mount_not_permitted(text):
            msg = f"Not allowed to mount {spec.name}: {text}"
            raise SetupFailedError(
                msg,
                translation_key="mount_not_permitted",
                translation_owner=TRANSLATION_OWNER,
                translation_args=[SHARES_DOCS_URL],
            )
        raise classify_mount_error(spec.share_type, text)

    async def update(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share again with changed settings.

        :param spec: The share with its new settings.
        :param password: The password of the share, decrypted.
        """
        await self.reload(spec, password)

    async def reload(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share again.

        :param spec: The share.
        :param password: The password of the share, decrypted.
        """
        await self._unmount(spec)
        await self.add(spec, password)

    async def remove(self, spec: NetworkShareSpec) -> None:
        """
        Unmount a share and remove its mountpoint; a share that is not mounted is fine.

        :param spec: The share.
        """
        await self._unmount(spec)
        with suppress(OSError):
            await asyncio.to_thread(os.rmdir, spec.path)

    async def get_states(self, specs: list[NetworkShareSpec]) -> dict[str, ShareState]:
        """
        Return for each share, by name, whether it is mounted.

        :param specs: The shares of this backend.
        """

        def _get_states() -> dict[str, ShareState]:
            table = read_mountinfo()
            return {
                spec.name: ShareState.PRESENT
                if is_mounted(spec.path, table)
                else ShareState.MISSING
                for spec in specs
            }

        return await asyncio.to_thread(_get_states)

    async def _unmount(self, spec: NetworkShareSpec) -> None:
        """
        Unmount a share, refusing a mountpoint that another user could have prepared.

        :param spec: The share.
        """
        try:
            await asyncio.to_thread(_check_own_mountpoint, MOUNT_ROOT, spec.path)
        except UnsafeFolderError as err:
            raise _unsafe_folder(spec, err) from err
        await unmount(spec.path, self.logger, _is_mounted)


def _probe_local_mount_support() -> tuple[dict[ShareType, list[str]], str | None]:
    """Return what this process can mount itself, and why when nothing (blocking)."""
    system = platform.system()
    # another system may not even know user ids
    euid = os.geteuid() if system in ("Linux", "Darwin") else None
    cap_eff = _read_cap_eff() if system == "Linux" else None
    return get_local_mount_support(system, euid, cap_eff, _has_helper)


def _is_mounted(path: str) -> bool:
    """Return whether a share is mounted on a path, without touching the share (blocking)."""
    return is_mounted(path, read_mountinfo())


def _read_cap_eff() -> int | None:
    """Return the effective capability set of this process, None when unknown (blocking)."""
    try:
        with open(PROC_STATUS_PATH, encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith("CapEff:"):
                    return int(line.split()[1], 16)
    except OSError, ValueError, IndexError:
        pass
    return None


def _has_helper(binary: str) -> bool:
    """Return whether a mount helper is installed, also outside the PATH (blocking)."""
    search_path = os.pathsep.join((os.environ.get("PATH", ""), "/sbin", "/usr/sbin"))
    return shutil.which(binary, path=search_path) is not None


def _prepare_mountpoint(root: str, path: str) -> None:
    """
    Create the mount root and a mountpoint in it where missing, and check both (blocking).

    :param root: The mount root.
    :param path: The mountpoint of a share, right below the root.
    :raises UnsafeFolderError: For a folder another user could have prepared.
    """
    with suppress(FileExistsError):
        Path(root).mkdir(mode=0o700)
    if stat.S_IMODE(_check_root(root, path).st_mode) != 0o700:
        # only this process may see into it, whatever the umask or an earlier mode
        try:
            Path(root).chmod(0o700)
        except OSError as err:
            raise UnsafeFolderError(root) from err
    with suppress(FileExistsError):
        Path(path).mkdir()
    if not stat.S_ISDIR(os.lstat(path).st_mode):
        raise UnsafeFolderError(path)


def _check_own_mountpoint(root: str, path: str) -> None:
    """
    Raise when a mountpoint is no folder of this backend; one that is not there is fine (blocking).

    :param root: The mount root.
    :param path: The mountpoint of a share, right below the root.
    :raises UnsafeFolderError: For a folder another user could have prepared.
    """
    try:
        _check_root(root, path)
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise UnsafeFolderError(path)
    except FileNotFoundError:
        # nothing is mounted on what is not there
        return
    except OSError as err:
        # what can not be looked at can not be trusted either
        raise UnsafeFolderError(path) from err


def _check_root(root: str, path: str) -> os.stat_result:
    """
    Return the details of the mount root, when it is a folder this process alone can change.

    :param root: The mount root.
    :param path: The mountpoint of a share, which must lie right below it.
    :raises UnsafeFolderError: For a root another user could have prepared.
    """
    if os.path.dirname(path) != root:
        raise UnsafeFolderError(path)
    info = os.lstat(root)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise UnsafeFolderError(root)
    return info


def _unsafe_folder(spec: NetworkShareSpec, err: UnsafeFolderError) -> SetupFailedError:
    """
    Return the error for a share whose mountpoint another user could have prepared.

    :param spec: The share.
    :param err: The refusal, naming the folder.
    """
    msg = f"Not mounting {spec.name} on {err}: another user could have prepared it"
    return SetupFailedError(
        msg,
        translation_key="mount_folder_unsafe",
        translation_owner=TRANSLATION_OWNER,
        translation_args=[str(err)],
    )
