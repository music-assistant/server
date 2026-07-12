"""NFS Filesystem Provider implementation."""

from __future__ import annotations

import platform
from pathlib import PurePosixPath

from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.process import check_output
from music_assistant.providers.filesystem_local import (
    LocalFileSystemProvider,
    exists,
    ismount,
    makedirs,
)

from .constants import CONF_EXPORT_PATH, CONF_HOST, CONF_NFS_VERSION, CONF_SUBFOLDER


class NFSFileSystemProvider(LocalFileSystemProvider):
    """
    Implementation of an NFS File System Provider.

    This is a wrapper around the local filesystem provider that mounts
    an NFS export to a temporary location. Once mounted, all file operations
    are handled by the base LocalFileSystemProvider.
    """

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        export_path = str(self.config.get_value(CONF_EXPORT_PATH))
        subfolder = str(self.config.get_value(CONF_SUBFOLDER) or "")
        if subfolder:
            return subfolder
        if export_path:
            return PurePosixPath(export_path).name
        return None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if not await exists(self.base_path):
            await makedirs(self.base_path)
        try:
            # unmount first to cleanup any unexpected state
            await self.unmount(ignore_error=True)
            await self.mount()
        except OSError as err:
            msg = f"NFS mount failed: {err}"
            raise SetupFailedError(msg) from err
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
            "mounted": await ismount(self.base_path),
        }

    async def mount(self) -> None:
        """Mount the NFS export to a temporary folder."""
        server = str(self.config.get_value(CONF_HOST))
        export_path = str(self.config.get_value(CONF_EXPORT_PATH))

        # build full export path including optional subfolder
        subfolder = str(self.config.get_value(CONF_SUBFOLDER) or "").strip()
        full_export = (
            str(PurePosixPath(export_path) / subfolder.lstrip("/")) if subfolder else export_path
        )

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
            f"{server}:{full_export}",
            self.base_path,
        ]

        self.logger.debug("Mounting %s:%s to %s", server, full_export, self.base_path)
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

        nfs_version = str(self.config.get_value(CONF_NFS_VERSION) or "")
        if nfs_version:
            options.append(f"vers={nfs_version}")

        return options

    async def unmount(self, ignore_error: bool = False) -> None:
        """Unmount the remote NFS export."""
        returncode: int
        output: bytes
        returncode, output = await check_output("umount", self.base_path)
        if returncode != 0 and not ignore_error:
            self.logger.warning("NFS unmount failed with error: %s", output.decode())
