"""Models for the storage controller, serialized as is by the API."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mashumaro import DataClassDictMixin, field_options
from music_assistant_models.translations import resolve_translation

from music_assistant.controllers.storage.constants import TRANSLATION_OWNER


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
    # in English; an API result carries it in the language of the client
    error: str | None = None
    # the translation of the error in the strings of the storage controller, never sent itself
    error_key: str | None = field(
        default=None, metadata=field_options(serialize="omit"), repr=False
    )
    error_args: list[str] = field(
        default_factory=list, metadata=field_options(serialize="omit"), repr=False
    )

    def __post_serialize__(self, d: dict[str, Any]) -> dict[str, Any]:
        """Localize the error when a resolver for the language of the client is active."""
        if self.error_key:
            localized = resolve_translation(
                f"errors.{self.error_key}",
                owner=TRANSLATION_OWNER,
                params=self.error_args or None,
            )
            if localized is not None:
                d["error"] = localized
        return d


@dataclass
class StorageInfo(DataClassDictMixin):
    """The storage locations a caller may see, plus what can be added."""

    locations: list[StorageLocation]
    can_mount_shares: bool
    mount_backend: MountBackend | None
    supported_share_types: list[ShareType]
    # the protocol versions a share can be pinned to, next to automatic; an empty list means
    # the version can not be chosen
    supported_share_versions: dict[ShareType, list[str]]
    can_add_local_folder: bool


@dataclass
class NetworkShareSpec(DataClassDictMixin):
    """A network share Music Assistant mounts, as stored in the settings (never sent as is)."""

    name: str
    share_type: ShareType
    server: str
    # the share name of a cifs share, the export path of an nfs share
    share: str
    # what mounted the share and where, fixed when the share was added
    backend: MountBackend
    path: str
    username: str | None = None
    # encrypted with the encryption key of the settings
    password: str | None = None
    version: str | None = None
    read_only: bool = False
