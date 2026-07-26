"""SMB filesystem provider for Music Assistant."""

from __future__ import annotations

import asyncio
import os
import platform
from typing import TYPE_CHECKING
from urllib.parse import quote

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME, VERBOSE_LOG_LEVEL
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.process import check_output
from music_assistant.helpers.util import get_ip_from_host
from music_assistant.providers.filesystem_local import (
    LocalFileSystemProvider,
    exists,
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

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_HOST = "host"
CONF_SHARE = "share"
CONF_SUBFOLDER = "subfolder"
CONF_SMB_VERSION = "smb_version"
CONF_CACHE_MODE = "cache_mode"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # base_path will be the path where we're going to mount the remote share
    base_path = f"/tmp/{config.instance_id}"  # noqa: S108
    return SMBFileSystemProvider(mass, manifest, config, base_path)


class SMBFileSystemProvider(LocalFileSystemProvider):
    """
    Implementation of an SMB File System Provider.

    Basically this is just a wrapper around the regular local files provider,
    except for the fact that it will mount a remote folder to a temporary location.
    We went for this OS-depdendent approach because there is no solid async-compatible
    smb library for Python (and we tried both pysmb and smbprotocol).
    """

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a (default) instance name postfix for this provider instance."""
        share = str(self.get_setup_value(CONF_SHARE))
        subfolder = str(self.get_setup_value(CONF_SUBFOLDER))
        if subfolder:
            return subfolder
        if share:
            return share
        return None

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        # connection details and content type are collected by the setup flow; surface the
        # (immutable) content type read-only so the sync options' depends_on chains resolve
        content_type = str(self.get_setup_value(CONF_CONTENT_TYPE, "music"))
        return (
            ConfigEntry(key=CONF_CONTENT_TYPE, type=ConfigEntryType.LABEL, value=content_type),
            ConfigEntry(
                key=CONF_CACHE_MODE,
                type=ConfigEntryType.STRING,
                required=False,
                advanced=True,
                default_value="loose",
                options=[
                    ConfigValueOption("strict"),
                    ConfigValueOption("loose"),
                    ConfigValueOption("none"),
                ],
            ),
            CONF_ENTRY_MISSING_ALBUM_ARTIST,
            CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
            CONF_ENTRY_LIBRARY_SYNC_TRACKS,
            CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
            CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
            CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
            CONF_ENTRY_PROPAGATE_GENRES,
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # validate the connection details before attempting to mount
        server = str(self.get_setup_value(CONF_HOST))
        if not await get_ip_from_host(server):
            msg = f"Unable to resolve {server}, make sure the address is resolvable."
            raise LoginFailed(
                msg,
                translation_key="host_unresolvable",
                translation_args=[server],
            )
        share = str(self.get_setup_value(CONF_SHARE))
        if not share or "/" in share or "\\" in share:
            msg = "Invalid share name"
            raise LoginFailed(msg)
        if not await exists(self.base_path):
            await makedirs(self.base_path)
        try:
            # do unmount first to cleanup any unexpected state
            await self.unmount(ignore_error=True)
            await self.mount()
        except OSError as err:
            msg = f"Connection failed for the given details: {err}"
            raise LoginFailed(msg) from err
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

    def _build_macos_mount_cmd(
        self, server: str, username: str, password: str | None, share: str, subfolder: str
    ) -> list[str]:
        """Build mount command for macOS."""
        mount_options = []

        # Add SMB version if specified
        smb_version = str(self.get_setup_value(CONF_SMB_VERSION) or "")
        if smb_version:
            # macOS uses different version format (e.g., smb2, smb3)
            if smb_version.startswith("3"):
                mount_options.extend(["-o", "protocol_vers_map=6"])  # SMB3
            elif smb_version.startswith("2"):
                mount_options.extend(["-o", "protocol_vers_map=4"])  # SMB2

        # Construct credentials in URL format
        # macOS mount_smbfs supports special characters in password when URL-encoded
        encoded_password = f":{quote(str(password), safe='')}" if password else ""

        return [
            "mount",
            "-t",
            "smbfs",
            *mount_options,
            f"//{username}{encoded_password}@{server}/{share}{subfolder}",
            self.base_path,
        ]

    def _build_linux_mount_cmd(
        self,
        server: str,
        username: str,
        password: str | None,
        share: str,
        subfolder: str,
        env_vars: dict[str, str],
    ) -> tuple[list[str], dict[str, str]]:
        """
        Build mount command for Linux.

        Uses the PASSWD environment variable to handle passwords with special characters
        (commas, etc.) that cannot be escaped on the command line.

        :param server: The SMB server hostname or IP.
        :param username: The username for authentication.
        :param password: The password for authentication (can contain special chars).
        :param share: The share name on the server.
        :param subfolder: Optional subfolder path within the share.
        :param env_vars: Environment variables dict to modify with PASSWD if needed.
        :returns: Tuple of (mount command args, modified env vars).
        """
        options = ["rw"]  # read-write access

        # We pass the password via the PASSWD environment variable to avoid
        # improperly escaped passwords with special characters.
        if username and username.lower() != "guest":
            options.append(f"username={username}")
            if password:
                env_vars["PASSWD"] = password
        else:
            # Guest/anonymous access
            options.append("guest")

        # SMB version for better compatibility and performance
        smb_version = str(self.get_setup_value(CONF_SMB_VERSION) or "")
        if smb_version:
            options.append(f"vers={smb_version}")

        # Cache mode for better performance
        cache_mode = str(self.config.get_value(CONF_CACHE_MODE) or "loose")
        options.append(f"cache={cache_mode}")

        # Case insensitive by default (standard for SMB) and other performance options.
        # Note: emoji and other 4-byte UTF-8 characters (U+10000+) in folder/file names
        # are NOT supported due to a Linux kernel limitation in the CIFS client's NLS layer.
        # Items with such characters will be skipped during library sync.
        options.extend(
            [
                "iocharset=utf8",
                "nocase",
                "file_mode=0755",
                "dir_mode=0755",
                "uid=0",
                "gid=0",
                "noperm",
                "nobrl",
                "mfsymlinks",
                "noserverino",
                "actimeo=30",
            ]
        )

        mount_cmd = [
            "mount",
            "-t",
            "cifs",
            "-o",
            ",".join(options),
            f"//{server}/{share}{subfolder}",
            self.base_path,
        ]
        return mount_cmd, env_vars

    async def _enumerate_files_for_sync(
        self,
        *,
        file_checksums: dict[str, str],
        cue_file_checksums: dict[str, str],
        cur_filenames: set[str],
        items_to_process: list[tuple[FileSystemItem, str | None]],
        unchanged_cue_items: list[FileSystemItem],
        cue_stems: set[str],
        root_scan_errors: list[OSError],
    ) -> None:
        """Override to remount and retry if the SMB mount drops during scan enumeration.

        The parent class aborts the entire library sync and marks the provider
        unavailable when ``os.scandir`` raises an OSError at the root mount point,
        which happens whenever the SMB server is temporarily unreachable ("Host is
        down", "Resource temporarily unavailable", etc.).

        This override catches root-level enumeration failures, unmounts and remounts
        the CIFS share with exponential backoff, and retries the scan. If all retry
        attempts fail, the errors are passed through to the parent's abort logic.
        """

        max_attempts = 3
        for attempt in range(max_attempts):
            # Check if mount is alive before starting the walk
            if not await ismount(self.base_path):
                self.logger.warning(
                    "SMB mount not available (attempt %d/%d), remounting...",
                    attempt + 1, max_attempts,
                )
                await self.unmount(ignore_error=True)
                await asyncio.sleep(1)
                await self.mount()

            root_scan_errors.clear()
            await super()._enumerate_files_for_sync(
                file_checksums=file_checksums,
                cue_file_checksums=cue_file_checksums,
                cur_filenames=cur_filenames,
                items_to_process=items_to_process,
                unchanged_cue_items=unchanged_cue_items,
                cue_stems=cue_stems,
                root_scan_errors=root_scan_errors,
            )

            if not root_scan_errors:
                return  # success

            self.logger.warning(
                "SMB root scan failed with %d error(s) (attempt %d/%d), "
                "unmounting and retrying in %ds...",
                len(root_scan_errors), attempt + 1, max_attempts,
                2 ** attempt,
            )
            root_scan_errors.clear()
            await self.unmount(ignore_error=True)
            await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s backoff

    async def mount(self) -> None:
        """Mount the SMB location to a temporary folder."""
        server = str(self.get_setup_value(CONF_HOST))
        username = str(self.get_setup_value(CONF_USERNAME) or "guest")
        password = self.get_setup_value(CONF_PASSWORD)
        # Type narrowing: password can be str or None
        password_str: str | None = str(password) if password is not None else None
        share = str(self.get_setup_value(CONF_SHARE))

        # handle optional subfolder
        subfolder = str(self.get_setup_value(CONF_SUBFOLDER) or "")
        if subfolder:
            subfolder = subfolder.replace("\\", "/")
            if not subfolder.startswith("/"):
                subfolder = "/" + subfolder
            subfolder = subfolder.removesuffix("/")

        env_vars = os.environ.copy()

        if platform.system() == "Darwin":
            mount_cmd = self._build_macos_mount_cmd(
                server, username, password_str, share, subfolder
            )
        elif platform.system() == "Linux":
            mount_cmd, env_vars = self._build_linux_mount_cmd(
                server, username, password_str, share, subfolder, env_vars
            )
        else:
            msg = f"SMB provider is not supported on {platform.system()}"
            raise LoginFailed(msg)

        self.logger.debug("Mounting //%s/%s%s to %s", server, share, subfolder, self.base_path)
        self.logger.log(VERBOSE_LOG_LEVEL, "Using mount command: %s", " ".join(mount_cmd))
        returncode, output = await check_output(*mount_cmd, env=env_vars)
        if returncode != 0:
            msg = f"SMB mount failed with error: {output.decode()}"
            raise LoginFailed(msg)

    async def unmount(self, ignore_error: bool = False) -> None:
        """Unmount the remote share."""
        returncode, output = await check_output("umount", self.base_path)
        if returncode != 0 and not ignore_error:
            self.logger.warning("SMB unmount failed with error: %s", output.decode())
