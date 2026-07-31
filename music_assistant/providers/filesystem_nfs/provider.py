"""NFS Filesystem Provider implementation."""

from __future__ import annotations

import os
import platform
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.process import check_output
from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.util import get_ip_from_host
from music_assistant.providers.filesystem_local import (
    LocalFileSystemProvider,
    exists,
    isdir,
    ismount,
    makedirs,
)
from music_assistant.providers.filesystem_local.constants import (
    CONF_CONTENT_TYPE,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    CONF_ENTRY_PROPAGATE_GENRES,
)

from .constants import CONF_EXPORT_PATH, CONF_HOST, CONF_NFS_VERSION, CONF_SUBFOLDER

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant


class NFSFileSystemProvider(LocalFileSystemProvider):
    """
    Implementation of an NFS File System Provider.

    This is a wrapper around the local filesystem provider that mounts
    an NFS export to a temporary location. Once mounted, all file operations
    are handled by the base LocalFileSystemProvider.
    """

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        base_path: str | None = None,
    ) -> None:
        """
        Initialize NFS FileSystem Provider.

        :raises SetupFailedError: If the configured subfolder would escape the mountpoint.
        """
        super().__init__(mass, manifest, config, base_path)
        # the export is mounted exactly as configured and the optional subfolder is a path
        # *inside* that mount, because NFSv3's rpc.mountd only hands out a filehandle for a
        # path that is itself exported. That splits the incoming base_path into two roles:
        # mount_path owns the mount lifecycle, base_path stays the scan/serve root the base
        # class works from.
        self.mount_path: str = self.base_path
        self._subfolder: str = str(self.get_setup_value(CONF_SUBFOLDER) or "").strip().lstrip("/")
        if not self._subfolder:
            return
        if not is_safe_path(self._subfolder, self.mount_path):
            msg = f"Invalid subfolder {self._subfolder}: must be a relative path inside the export"
            raise SetupFailedError(
                msg,
                translation_key="invalid_subfolder",
                translation_owner=self.translation_owner,
                translation_args=[self._subfolder],
            )
        self.base_path = os.path.normpath(os.path.join(self.mount_path, self._subfolder))

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        # connection details and content type are collected by the setup flow; surface the
        # (immutable) content type read-only so the sync options' depends_on chains resolve
        content_type = str(self.get_setup_value(CONF_CONTENT_TYPE, "music"))
        return (
            ConfigEntry(key=CONF_CONTENT_TYPE, type=ConfigEntryType.LABEL, value=content_type),
            CONF_ENTRY_MISSING_ALBUM_ARTIST,
            CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
            CONF_ENTRY_LIBRARY_SYNC_TRACKS,
            CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
            CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
            CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
            CONF_ENTRY_PROPAGATE_GENRES,
        )

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        export_path = str(self.get_setup_value(CONF_EXPORT_PATH))
        subfolder = str(self.get_setup_value(CONF_SUBFOLDER) or "")
        if subfolder:
            return subfolder
        if export_path:
            return PurePosixPath(export_path).name
        return None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # validate the connection details before attempting to mount
        server = str(self.get_setup_value(CONF_HOST))
        if not await get_ip_from_host(server):
            msg = f"Unable to resolve {server}, make sure the address is resolvable."
            raise SetupFailedError(
                msg,
                translation_key="host_unresolvable",
                translation_args=[server],
            )
        export_path = str(self.get_setup_value(CONF_EXPORT_PATH))
        if not export_path or not export_path.startswith("/") or not is_safe_path(export_path):
            msg = "Invalid export path: must be an absolute path starting with /"
            raise SetupFailedError(msg)
        if not await exists(self.mount_path):
            await makedirs(self.mount_path)
        try:
            # unmount first to cleanup any unexpected state
            await self.unmount(ignore_error=True)
            await self.mount()
        except OSError as err:
            msg = f"NFS mount failed: {err}"
            raise SetupFailedError(msg) from err
        # the subfolder lives on the remote share, so it can only be checked once mounted.
        # without this the failure is silent: check_write_access swallows every error and
        # the sync then just reports an empty library.
        if self._subfolder and not await isdir(self.base_path):
            await self.unmount(ignore_error=True)
            msg = f"Subfolder {self._subfolder} does not exist in the NFS export"
            raise SetupFailedError(
                msg,
                translation_key="subfolder_not_found",
                translation_owner=self.translation_owner,
                translation_args=[self._subfolder],
            )
        await self.check_write_access()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        await self.unmount(ignore_error=True)

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        return {
            **await super().get_diagnostics(),
            "mounted": await ismount(self.mount_path),
        }

    async def mount(self) -> None:
        """Mount the NFS export to a temporary folder."""
        server = str(self.get_setup_value(CONF_HOST))
        export_path = str(self.get_setup_value(CONF_EXPORT_PATH))

        if platform.system() not in ("Linux", "Darwin"):
            msg = f"NFS provider is not supported on {platform.system()}"
            raise SetupFailedError(msg)

        mount_options = self._get_mount_options()
        mount_cmd = [
            "mount",
            "-t",
            "nfs",
            "-o",
            ",".join(mount_options),
            f"{server}:{export_path}",
            self.mount_path,
        ]

        self.logger.debug("Mounting %s:%s to %s", server, export_path, self.mount_path)
        self.logger.log(VERBOSE_LOG_LEVEL, "Using mount command: %s", " ".join(mount_cmd))
        returncode: int
        output: bytes
        returncode, output = await check_output(*mount_cmd)
        if returncode != 0:
            msg = f"NFS mount failed with error: {output.decode()}"
            raise SetupFailedError(msg)

    def _get_mount_options(self) -> list[str]:
        """Get platform-specific NFS mount options."""
        if platform.system() == "Darwin":
            options = ["resvport", "noatime", "soft", "timeo=30", "retrans=5"]
        else:
            options = ["noatime", "nolock", "tcp", "soft", "timeo=30", "retrans=5"]

        nfs_version = str(self.get_setup_value(CONF_NFS_VERSION) or "")
        if nfs_version:
            options.append(f"vers={nfs_version}")

        return options

    async def unmount(self, ignore_error: bool = False) -> None:
        """Unmount the remote NFS export."""
        returncode: int
        output: bytes
        returncode, output = await check_output("umount", self.mount_path)
        if returncode != 0 and not ignore_error:
            self.logger.warning("NFS unmount failed with error: %s", output.decode())
