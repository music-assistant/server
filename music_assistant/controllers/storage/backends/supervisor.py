"""
Network shares mounted by the Home Assistant Supervisor.

A media mount of the Supervisor shows up at ``/media/<name>`` in every app that maps the media
folder, behind an automount trigger that mounts the share on first access. The Supervisor only
answers a request that creates, changes or reloads a mount once the share answered, and it does
not keep a new mount whose share did not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from music_assistant_models.errors import SetupFailedError

from music_assistant.controllers.storage.backends.base import BackendUnavailable, ShareMounter
from music_assistant.controllers.storage.backends.mountinfo import SUPERVISOR_MEDIA_PATH
from music_assistant.controllers.storage.helpers import share_key
from music_assistant.controllers.storage.models import MountBackend, NetworkShareSpec, ShareType
from music_assistant.helpers.hassio import SupervisorError, supervisor_request
from music_assistant.helpers.mount import classify_mount_error, error_summary

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# the only CIFS versions the Supervisor lets a mount pin, it negotiates any other one itself
SUPERVISOR_CIFS_VERSIONS: Final[tuple[str, ...]] = ("1.0", "2.0")
# a request that mounts waits for the Supervisor, which waits up to 90 seconds for systemd
MOUNT_REQUEST_TIMEOUT: Final[float] = 120
# the usage of a mount that shows up in the media folder of the apps
MEDIA_USAGE: Final[str] = "media"


async def create_supervisor_mounter(mass: MusicAssistant) -> SupervisorMounter:
    """
    Return the mounter of the Supervisor, when this app may manage the Supervisor's mounts.

    :param mass: The Music Assistant instance.
    :raises BackendUnavailable: When there is no Supervisor, or it refuses access to its mounts.
    """
    if not mass.running_as_hass_addon:
        msg = "not running under a Supervisor"
        raise BackendUnavailable(msg)
    try:
        await supervisor_request(mass, "get", "/mounts")
    except SupervisorError as err:
        if err.status in (401, 403):
            msg = (
                f"the Supervisor refused access to its mounts (HTTP {err.status}), "
                "the app has no manager role"
            )
            raise BackendUnavailable(msg) from err
        msg = f"the Supervisor did not answer: {err.message}"
        raise BackendUnavailable(msg) from err
    return SupervisorMounter(mass)


class SupervisorMounter(ShareMounter):
    """Mounts network shares as media mounts of the Supervisor."""

    backend = MountBackend.SUPERVISOR

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the mounter.

        :param mass: The Music Assistant instance.
        """
        super().__init__({ShareType.CIFS: list(SUPERVISOR_CIFS_VERSIONS), ShareType.NFS: []})
        self.mass = mass

    def get_path(self, name: str) -> str:
        """
        Return where a share with this name is mounted.

        :param name: The name of the share.
        """
        return f"{SUPERVISOR_MEDIA_PATH}/{name}"

    async def find_mount(self, share_type: ShareType, server: str, share: str) -> str | None:
        """
        Return the path of a media mount of a share that the Supervisor has already.

        Such a mount was added in Home Assistant, or by Music Assistant for a share it no longer
        manages. None when the Supervisor has no media mount of the share.

        :param share_type: The protocol of the share.
        :param server: The hostname or IP address of the server.
        :param share: The share name of a cifs share, the export path of an nfs share.
        :raises SetupFailedError: When the Supervisor does not list its mounts.
        """
        wanted = share_key(share_type, server, share)
        for mount in await self._get_mounts():
            mount_share = mount.get("share" if mount.get("type") == ShareType.CIFS else "path")
            if (
                mount.get("usage") == MEDIA_USAGE
                and share_key(str(mount.get("type")), str(mount.get("server")), str(mount_share))
                == wanted
            ):
                return self.get_path(mount["name"])
        return None

    async def assign_name(self, spec: NetworkShareSpec, taken: Collection[str]) -> NetworkShareSpec:
        """
        Return a new share with a name that no mount of the Supervisor has, and its path.

        :param spec: The new share, without name and path.
        :param taken: The names of the shares Music Assistant manages.
        """
        in_use = {mount["name"] for mount in await self._get_mounts()}
        # anything in the media folder blocks a mount on its path when it holds files
        in_use |= await asyncio.to_thread(_list_media_folder)
        return await super().assign_name(spec, {*taken, *in_use})

    async def add(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share that is not mounted.

        :param spec: The share.
        :param password: The password of the share, decrypted.
        """
        await self._mount_request(spec, "post", "/mounts", _mount_payload(spec, password))

    async def update(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share again with changed settings.

        :param spec: The share with its new settings.
        :param password: The password of the share, decrypted.
        """
        payload = _mount_payload(spec, password)
        await self._mount_request(spec, "put", f"/mounts/{spec.name}", payload)

    async def reload(self, spec: NetworkShareSpec, password: str | None) -> None:
        """
        Mount a share again, also when it is gone from the Supervisor.

        :param spec: The share.
        :param password: The password of the share, decrypted.
        """
        try:
            await supervisor_request(
                self.mass, "post", f"/mounts/{spec.name}/reload", timeout=MOUNT_REQUEST_TIMEOUT
            )
        except SupervisorError as err:
            if err.status != 404:
                raise classify_mount_error(spec.share_type, err.message) from err
            # removed in Home Assistant: create it again
            await self.add(spec, password)

    async def remove(self, spec: NetworkShareSpec) -> None:
        """
        Remove the mount of a share from the Supervisor; a mount that is gone is fine.

        :param spec: The share.
        """
        try:
            await supervisor_request(
                self.mass, "delete", f"/mounts/{spec.name}", timeout=MOUNT_REQUEST_TIMEOUT
            )
        except SupervisorError as err:
            if err.status == 404:
                return
            msg = f"Unable to remove the mount of {spec.name}: {err.message}"
            raise SetupFailedError(
                msg, translation_key="unmount_failed", translation_args=[error_summary(err.message)]
            ) from err

    async def get_unmounted(self, specs: list[NetworkShareSpec]) -> list[NetworkShareSpec]:
        """
        Return the shares the Supervisor has no mount for.

        :param specs: The shares of this backend.
        """
        names = {mount["name"] for mount in await self._get_mounts()}
        return [spec for spec in specs if spec.name not in names]

    async def _get_mounts(self) -> list[dict[str, Any]]:
        """Return the mounts of the Supervisor, secrets left out."""
        try:
            data = await supervisor_request(self.mass, "get", "/mounts")
        except SupervisorError as err:
            msg = f"Unable to get the mounts of the Supervisor: {err.message}"
            raise SetupFailedError(
                msg, translation_key="mount_failed", translation_args=[error_summary(err.message)]
            ) from err
        return list(data.get("mounts", [])) if isinstance(data, dict) else []

    async def _mount_request(
        self, spec: NetworkShareSpec, method: str, path: str, payload: dict[str, Any]
    ) -> None:
        """
        Send a request that mounts a share, raising the error to show when it failed.

        :param spec: The share.
        :param method: The HTTP method of the request.
        :param path: The path of the API endpoint.
        :param payload: The mount as the Supervisor takes it.
        """
        try:
            await supervisor_request(
                self.mass, method, path, json_data=payload, timeout=MOUNT_REQUEST_TIMEOUT
            )
        except SupervisorError as err:
            raise classify_mount_error(spec.share_type, err.message) from err


def _mount_payload(spec: NetworkShareSpec, password: str | None) -> dict[str, Any]:
    """
    Return a share as a media mount of the Supervisor.

    :param spec: The share.
    :param password: The password of the share, decrypted.
    """
    payload: dict[str, Any] = {
        "name": spec.name,
        "type": spec.share_type.value,
        "usage": MEDIA_USAGE,
        "server": spec.server,
        "read_only": spec.read_only,
    }
    if spec.share_type == ShareType.NFS:
        payload["path"] = spec.share
        return payload
    payload["share"] = spec.share
    if spec.username:
        # the Supervisor takes both or neither, and mounts as guest without them
        payload["username"] = spec.username
        payload["password"] = password or ""
    if spec.version in SUPERVISOR_CIFS_VERSIONS:
        payload["version"] = spec.version
    return payload


def _list_media_folder() -> set[str]:
    """Return the names in the media folder (blocking)."""
    try:
        return {entry.name for entry in Path(SUPERVISOR_MEDIA_PATH).iterdir()}
    except OSError:
        return set()
