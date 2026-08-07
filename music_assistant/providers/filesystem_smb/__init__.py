"""SMB filesystem provider for Music Assistant."""

from __future__ import annotations

import os
import platform
from typing import TYPE_CHECKING
from urllib.parse import quote

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed, SetupFailedError, UnsupportedSystemError

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME, VERBOSE_LOG_LEVEL
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.mount import error_summary, unmount
from music_assistant.helpers.process import check_output
from music_assistant.helpers.util import get_ip_from_host
from music_assistant.providers.filesystem_local import (
    LocalFileSystemProvider,
    ismount,
    makedirs,
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

# lowercase fragments that both mount tools (Linux mount.cifs and macOS mount_smbfs) emit when
# the server rejected the credentials - only those must be reported back as an auth problem
_AUTH_FAILURE_MARKERS = (
    "permission denied",
    "authentication error",
    "nt_status_logon_failure",
    "nt_status_access_denied",
    "nt_status_account_disabled",
    "nt_status_account_locked_out",
    "nt_status_password_expired",
    "nt_status_wrong_password",
)


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
        entries = await super().get_config_entries()
        return (
            *entries,
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
        )

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
        share = str(self.get_setup_value(CONF_SHARE))
        if not share or "/" in share or "\\" in share:
            msg = "Invalid share name"
            raise SetupFailedError(msg)
        # the mount point may already exist; checking first is not reliable because
        # reading the path fails while the server is unreachable
        await makedirs(self.base_path, exist_ok=True)
        try:
            # do unmount first to cleanup any unexpected state
            await unmount(self.base_path, self.logger)
            await self.mount()
        except OSError as err:
            msg = f"Unable to run the mount command: {err}"
            raise SetupFailedError(msg) from err
        await self.check_write_access()

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        await unmount(self.base_path, self.logger)

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        return {
            **await super().get_diagnostics(),
            "mounted": await ismount(self.base_path),
        }

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
            raise UnsupportedSystemError(msg)

        self.logger.debug("Mounting //%s/%s%s to %s", server, share, subfolder, self.base_path)
        self.logger.log(VERBOSE_LOG_LEVEL, "Using mount command: %s", " ".join(mount_cmd))
        returncode, output = await check_output(*mount_cmd, env=env_vars)
        if returncode != 0:
            raise _mount_error(output.decode().strip())

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


def _mount_error(output: str) -> SetupFailedError | LoginFailed:
    """
    Return the error to raise for a failed mount command.

    :param output: The (combined) output of the mount command.
    """
    lowered = output.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return LoginFailed(f"SMB mount failed with error: {output}")
    return SetupFailedError(
        f"SMB mount failed with error: {output}",
        translation_key="mount_failed",
        translation_args=[error_summary(output)],
    )
