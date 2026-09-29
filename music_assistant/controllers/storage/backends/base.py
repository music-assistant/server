"""The interface of the backends that mount the network shares Music Assistant manages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import replace
from enum import StrEnum

from music_assistant.controllers.storage.helpers import allocate_share_name
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType


class BackendUnavailable(Exception):
    """A mount backend can not be used on this server; the message says why."""


class ShareState(StrEnum):
    """What a mount backend has for a network share Music Assistant manages."""

    PRESENT = "present"  # the mount of the share
    FAILED = "failed"  # the mount of the share, which did not mount: it needs to be mounted again
    MISSING = "missing"  # nothing: the share needs to be mounted
    CHANGED = "changed"  # a mount under its name that is another share now, not ours to touch


class ShareMounter(ABC):
    """
    Mounts the network shares of one mount backend.

    Every method that mounts raises the error to show for a share that could not be mounted.
    """

    backend: MountBackend

    def __init__(self, supported_versions: dict[ShareType, list[str]]) -> None:
        """
        Initialize the mounter.

        :param supported_versions: The share types this backend can mount, each with the
            protocol versions a share can be pinned to (empty when it can not be chosen).
        """
        self.supported_versions = supported_versions

    @abstractmethod
    def get_path(self, name: str) -> str:
        """
        Return where a share with this name is mounted.

        :param name: The name of the share.
        """

    async def find_mount(self, share_type: ShareType, server: str, share: str) -> str | None:
        """
        Return the path of a mount of a share that the backend has without Music Assistant.

        :param share_type: The protocol of the share.
        :param server: The hostname or IP address of the server.
        :param share: The share name of a cifs share, the export path of an nfs share.
        """
        return None

    async def assign_name(self, spec: NetworkShareSpec, taken: Collection[str]) -> NetworkShareSpec:
        """
        Return a new share with its name and path.

        :param spec: The new share, without name and path.
        :param taken: The names of the shares Music Assistant manages.
        """
        name = allocate_share_name(spec.share_type, spec.share, taken)
        return replace(spec, name=name, path=self.get_path(name))

    @abstractmethod
    async def add(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share that is not mounted.

        :param spec: The share.
        :param password: The password of the share, decrypted.
        """

    @abstractmethod
    async def update(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share again with changed settings.

        :param spec: The share with its new settings.
        :param password: The password of the share, decrypted.
        """

    @abstractmethod
    async def reload(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share again, also when it is gone from the backend.

        :param spec: The share.
        :param password: The password of the share, decrypted.
        """

    @abstractmethod
    async def remove(self, spec: NetworkShareSpec) -> None:
        """
        Unmount a share for good; a share that is not mounted is fine.

        :param spec: The share.
        """

    @abstractmethod
    async def get_states(self, specs: list[NetworkShareSpec]) -> dict[str, ShareState]:
        """
        Return what the backend has for each share, by name.

        :param specs: The shares of this backend.
        """
