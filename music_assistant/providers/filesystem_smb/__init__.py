"""SMB filesystem provider for Music Assistant."""

from __future__ import annotations

import os
import platform
from typing import TYPE_CHECKING
from urllib.parse import quote

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME, VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import check_output
from music_assistant.helpers.util import get_ip_from_host
from music_assistant.providers.filesystem_local import LocalFileSystemProvider, exists, makedirs
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_CONTENT_TYPE_READ_ONLY,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
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
    # check if valid dns name is given for the host
    server = str(config.get_value(CONF_HOST))
    if not await get_ip_from_host(server):
        msg = f"Unable to resolve {server}, make sure the address is resolveable."
        raise LoginFailed(msg)
    # check if share is valid
    share = str(config.get_value(CONF_SHARE))
    if not share or "/" in share or "\\" in share:
        msg = "Invalid share name"
        raise LoginFailed(msg)
    # base_path will be the path where we're going to mount the remote share
    base_path = f"/tmp/{config.instance_id}"  # noqa: S108
    return SMBFileSystemProvider(mass, manifest, config, base_path)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    base_entries = (
        ConfigEntry(
            key=CONF_HOST,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The (fqdn) hostname of the SMB/CIFS/DFS server to connect to."
            "For example mynas.local.",
        ),
        ConfigEntry(
            key=CONF_SHARE,
            type=ConfigEntryType.STRING,
            label="Share",
            required=True,
            description="The name of the share/service you'd like to connect to on "
            "the remote host, For example 'media'.",
        ),
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=False,
            default_value="guest",
            description="The username to authenticate to the remote server. "
            "Leave as 'guest' or empty for anonymous access.",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            default_value=None,
            description="The password to authenticate to the remote server. "
            "Leave empty for anonymous/guest access.",
        ),
        ConfigEntry(
            key=CONF_SUBFOLDER,
            type=ConfigEntryType.STRING,
            label="Subfolder",
            required=False,
            default_value="",
            description="[optional] Use if your music is stored in a sublevel of the share. "
            "E.g. 'collections' or 'albums/A-K'.",
        ),
        ConfigEntry(
            key=CONF_SMB_VERSION,
            type=ConfigEntryType.STRING,
            label="SMB Version",
            required=False,
            category="advanced",
            default_value="3.0",
            options=[
                ConfigValueOption("Auto", ""),
                ConfigValueOption("SMB 1.0", "1.0"),
                ConfigValueOption("SMB 2.0", "2.0"),
                ConfigValueOption("SMB 2.1", "2.1"),
                ConfigValueOption("SMB 3.0", "3.0"),
                ConfigValueOption("SMB 3.1.1", "3.1.1"),
            ],
            description="The SMB protocol version to use. SMB 3.0 or higher is recommended for "
            "better performance and security. Use Auto to let the system negotiate.",
        ),
        ConfigEntry(
            key=CONF_CACHE_MODE,
            type=ConfigEntryType.STRING,
            label="Cache Mode",
            required=False,
            category="advanced",
            default_value="loose",
            options=[
                ConfigValueOption("Strict", "strict"),
                ConfigValueOption("Loose (Recommended)", "loose"),
                ConfigValueOption("None", "none"),
            ],
            description="Cache mode affects performance and consistency. "
            "'Loose' provides better performance for read-heavy workloads "
            "and is recommended for music libraries.",
        ),
        CONF_ENTRY_MISSING_ALBUM_ARTIST,
        CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_TRACKS,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
        CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    )

    if instance_id is None or values is None:
        return (
            CONF_ENTRY_CONTENT_TYPE,
            *base_entries,
        )
    return (
        *base_entries,
        CONF_ENTRY_CONTENT_TYPE_READ_ONLY,
    )


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
        share = str(self.config.get_value(CONF_SHARE))
        subfolder = str(self.config.get_value(CONF_SUBFOLDER))
        if subfolder:
            return subfolder
        elif share:
            return share
        return None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        if not await exists(self.base_path):
            await makedirs(self.base_path)
        try:
            # do unmount first to cleanup any unexpected state
            await self.unmount(ignore_error=True)
            await self.mount()
        except Exception as err:
            msg = f"Connection failed for the given details: {err}"
            raise LoginFailed(msg) from err
        await self.check_write_access()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        await self.unmount()

    async def mount(self) -> None:
        """Mount the SMB location to a temporary folder."""
        server = str(self.config.get_value(CONF_HOST))
        username = str(self.config.get_value(CONF_USERNAME) or "guest")
        password = self.config.get_value(CONF_PASSWORD)
        # Type narrowing: password can be str or None
        password_str: str | None = str(password) if password is not None else None
        share = str(self.config.get_value(CONF_SHARE))

        # handle optional subfolder
        subfolder = str(self.config.get_value(CONF_SUBFOLDER) or "")
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
            mount_cmd = self._build_linux_mount_cmd(
                server, username, password_str, share, subfolder
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

    def _build_macos_mount_cmd(
        self, server: str, username: str, password: str | None, share: str, subfolder: str
    ) -> list[str]:
        """Build mount command for macOS."""
        mount_options = []

        # Add SMB version if specified
        smb_version = str(self.config.get_value(CONF_SMB_VERSION) or "")
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
        self, server: str, username: str, password: str | None, share: str, subfolder: str
    ) -> list[str]:
        """Build mount command for Linux."""
        options = ["rw"]  # read-write access

        # Handle username and password
        if username and username.lower() != "guest":
            options.append(f"username={username}")
            if password:
                options.append(f"password={password}")
        else:
            # Guest/anonymous access
            options.append("guest")

        # SMB version for better compatibility and performance
        smb_version = str(self.config.get_value(CONF_SMB_VERSION) or "")
        if smb_version:
            options.append(f"vers={smb_version}")

        # Cache mode for better performance
        cache_mode = str(self.config.get_value(CONF_CACHE_MODE) or "loose")
        options.append(f"cache={cache_mode}")

        # Case insensitive by default (standard for SMB) and other performance options
        options.extend(
            [
                "nocase",
                "file_mode=0755",
                "dir_mode=0755",
                "uid=0",
                "gid=0",
                "iocharset=utf8",
                "noperm",
                "nobrl",
                "mfsymlinks",
                "noserverino",
                "actimeo=30",
            ]
        )

        return [
            "mount",
            "-t",
            "cifs",
            "-o",
            ",".join(options),
            f"//{server}/{share}{subfolder}",
            self.base_path,
        ]

    async def unmount(self, ignore_error: bool = False) -> None:
        """Unmount the remote share."""
        returncode, output = await check_output("umount", self.base_path)
        if returncode != 0 and not ignore_error:
            self.logger.warning("SMB unmount failed with error: %s", output.decode())
