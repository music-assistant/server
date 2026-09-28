"""Models for the storage controller, serialized as is by the API."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mashumaro import DataClassDictMixin


class StorageUsage(StrEnum):
    """What a storage location is used for."""

    MEDIA = "media"  # anything a music source can use
    DATA = "data"  # the server's own data directory
    CACHE = "cache"  # the server's own cache directory


class StorageKind(StrEnum):
    """Where a storage location comes from."""

    BUILTIN_MEDIA = "builtin_media"
    CONTAINER_VOLUME = "container_volume"
    NETWORK_SHARE = "network_share"
    REMOVABLE = "removable"
    LOCAL_DISK = "local_disk"
    MANUAL = "manual"


class ShareType(StrEnum):
    """Protocol of a network share."""

    CIFS = "cifs"
    NFS = "nfs"


class MountBackend(StrEnum):
    """What mounts a managed network share."""

    SUPERVISOR = "supervisor"
    LOCAL_MOUNT = "local_mount"


@dataclass
class StorageLocation(DataClassDictMixin):
    """A storage location the server can see, identified by its path."""

    path: str
    name: str
    usage: StorageUsage
    kind: StorageKind
    available: bool
    read_only: bool = False
    # created by Music Assistant (a network share or a registered folder), so it can be removed
    managed: bool = False
    backend: MountBackend | None = None
    fstype: str | None = None
    mountpoint: str | None = None
    share_name: str | None = None
    share_type: ShareType | None = None
    server: str | None = None
    share: str | None = None
    username: str | None = None
    version: str | None = None
    free_space_gb: float | None = None
    total_space_gb: float | None = None
    used_space_gb: float | None = None
    error: str | None = None


@dataclass
class StorageInfo(DataClassDictMixin):
    """The storage locations a caller may see, plus what can be added."""

    locations: list[StorageLocation]
    can_mount_shares: bool
    mount_backend: MountBackend | None
    supported_share_types: list[ShareType]
    can_add_local_folder: bool
