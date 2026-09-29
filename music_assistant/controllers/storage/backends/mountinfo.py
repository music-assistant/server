"""
Discovery of the media mounts in the mount table of the server process (Linux only).

Every line of ``/proc/self/mountinfo`` describes one mount:
``id parent major:minor root mountpoint options [optional fields] - fstype source superoptions``.
Inside a container the table holds the volumes mapped into it; under a Home Assistant Supervisor
it also holds every network share and drive the Supervisor mounted below ``/media``. The
Supervisor mounts a network share on first access: until then the table only holds an automount
trigger (``autofs``) on its mountpoint, and the real mount is listed on top of it afterwards.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from music_assistant.controllers.storage.helpers import is_within
from music_assistant.controllers.storage.models import StorageKind

MOUNTINFO_PATH: Final[str] = "/proc/self/mountinfo"

# filesystems that can hold a music collection: never the container root (overlay) or a
# pseudo filesystem (tmpfs, proc, sysfs, cgroup, devpts, squashfs, ...)
MEDIA_FSTYPES: Final[frozenset[str]] = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "f2fs",
        "zfs",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
        "fuseblk",
        "hfsplus",
        "apfs",
        "iso9660",
        "udf",
        "cifs",
        "smb3",
        "nfs",
        "nfs4",
        "virtiofs",
        "9p",
        # Docker Desktop for Mac reports its bind mounts with this type
        "fakeowner",
    }
)
MEDIA_FSTYPE_PREFIX: Final[str] = "fuse."
AUTOMOUNT_FSTYPE: Final[str] = "autofs"
NETWORK_FSTYPES: Final[frozenset[str]] = frozenset({"cifs", "smb3", "nfs", "nfs4"})
REMOVABLE_FSTYPES: Final[frozenset[str]] = frozenset(
    {"vfat", "exfat", "ntfs", "ntfs3", "hfsplus", "iso9660", "udf"}
)
# system paths never hold a media location; /tmp is where the SMB and NFS music sources mount
# their share for their own use
SYSTEM_PATHS: Final[tuple[str, ...]] = (
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/etc",
    "/var",
    "/usr",
    "/boot",
    "/efi",
    "/snap",
    "/tmp",  # noqa: S108
)
# the Home Assistant add-on folders, which a container never uses for media
CONTAINER_RESERVED_PATHS: Final[tuple[str, ...]] = (
    "/data",
    "/ssl",
    "/config",
    "/addons",
    "/backup",
    "/share",
)
SUPERVISOR_MEDIA_PATH: Final[str] = "/media"

_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class MediaMount:
    """A mount that can hold media."""

    mountpoint: str
    fstype: str
    read_only: bool
    kind: StorageKind


def parse_mountinfo(
    text: str,
    *,
    excluded_paths: Iterable[str],
    in_container: bool,
    supervisor: bool,
) -> list[MediaMount]:
    """
    Return the mounts in a mount table that can hold media, classified by kind.

    The result can still contain files bound into a container (the caller keeps directories
    only) and dormant automount triggers, which are listed as network shares with fstype autofs.

    :param text: Contents of a mountinfo file.
    :param excluded_paths: Absolute paths that are never a media location, nor anything below
        them (the server's own data and cache directories).
    :param in_container: Whether the server runs in a container.
    :param supervisor: Whether the server runs under a Home Assistant Supervisor.
    """
    mounts = _parse_table(text)
    excluded = [
        *SYSTEM_PATHS,
        *excluded_paths,
        *(CONTAINER_RESERVED_PATHS if in_container else ()),
    ]
    return [
        MediaMount(
            mountpoint=mountpoint,
            fstype=fstype,
            read_only=read_only,
            kind=_classify(mountpoint, fstype, in_container, supervisor),
        )
        for mountpoint, (fstype, read_only) in mounts.items()
        if (_is_media_fstype(fstype) or fstype == AUTOMOUNT_FSTYPE)
        and mountpoint != "/"
        and not any(is_within(mountpoint, path) for path in excluded)
    ]


def parse_mountpoints(text: str) -> set[str]:
    """
    Return every mountpoint in a mount table that has a filesystem mounted on it.

    A dormant automount trigger does not count.

    :param text: Contents of a mountinfo file.
    """
    return {
        mountpoint
        for mountpoint, (fstype, _read_only) in _parse_table(text).items()
        if fstype != AUTOMOUNT_FSTYPE
    }


def find_share_mount(text: str, mountpoint: str) -> MediaMount | None:
    """
    Return the network share mounted on a mountpoint, None when nothing is mounted there.

    Unlike discovery this also finds a mount below a system path such as /tmp. A share that is
    only behind its dormant automount trigger is returned with fstype autofs.

    :param text: Contents of a mountinfo file.
    :param mountpoint: Where the share is mounted.
    """
    if (mount := _parse_table(text).get(mountpoint)) is None:
        return None
    fstype, read_only = mount
    return MediaMount(mountpoint, fstype, read_only, StorageKind.NETWORK_SHARE)


def is_mounted(path: str, text: str) -> bool:
    """
    Return whether a filesystem is mounted on a path (blocking).

    Goes by the mount table where the system has one. Without one (macOS) the path must be a
    mountpoint by itself.

    :param path: The path to check.
    :param text: Contents of a mountinfo file, empty on a system without one.
    """
    # the mount table rather than os.path.ismount, which misses a bind mount of a folder on the
    # filesystem it is mounted on
    if text:
        return path in parse_mountpoints(text)
    return os.path.ismount(path)


def read_mountinfo() -> str:
    """Return the mount table of the server process, empty on a system without one (blocking)."""
    try:
        with open(MOUNTINFO_PATH, encoding="utf-8", errors="replace") as file:
            return file.read()
    except FileNotFoundError:
        return ""


def _parse_table(text: str) -> dict[str, tuple[str, bool]]:
    """Return the fstype and read-only state of the mount on each mountpoint of a mount table."""
    # a later mount on the same mountpoint hides the earlier one
    mounts: dict[str, tuple[str, bool]] = {}
    for line in text.splitlines():
        if (parsed := _parse_line(line)) is not None:
            mountpoint, fstype, read_only = parsed
            mounts[mountpoint] = (fstype, read_only)
    return mounts


def _parse_line(line: str) -> tuple[str, str, bool] | None:
    """Return the mountpoint, fstype and read-only state of a mountinfo line."""
    fields = line.split()
    try:
        # the optional fields end with a lone dash
        separator = fields.index("-", 6)
    except ValueError:
        return None
    if len(fields) < separator + 3:
        return None
    mountpoint = _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), fields[4])
    fstype = fields[separator + 1]
    super_options = fields[separator + 3] if len(fields) > separator + 3 else ""
    read_only = "ro" in fields[5].split(",") or "ro" in super_options.split(",")
    return mountpoint, fstype, read_only


def _is_media_fstype(fstype: str) -> bool:
    """Return whether a filesystem type can hold a music collection."""
    return fstype in MEDIA_FSTYPES or fstype.startswith(MEDIA_FSTYPE_PREFIX)


def _classify(mountpoint: str, fstype: str, in_container: bool, supervisor: bool) -> StorageKind:
    """Return where a media mount comes from."""
    if fstype in NETWORK_FSTYPES or fstype == AUTOMOUNT_FSTYPE:
        return StorageKind.NETWORK_SHARE
    if supervisor and mountpoint == SUPERVISOR_MEDIA_PATH:
        return StorageKind.BUILTIN_MEDIA
    if fstype in REMOVABLE_FSTYPES:
        return StorageKind.REMOVABLE
    # the Supervisor and desktop Linux automount drives below /media, while a plain container
    # has its volumes mapped there
    if mountpoint.startswith(f"{SUPERVISOR_MEDIA_PATH}/") and (supervisor or not in_container):
        return StorageKind.REMOVABLE
    return StorageKind.CONTAINER_VOLUME if in_container else StorageKind.LOCAL_DISK
