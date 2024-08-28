"""SMB filesystem provider for Music Assistant."""

from __future__ import annotations

import os
import platform
from typing import TYPE_CHECKING

from music_assistant.common.helpers.util import get_ip_from_host
from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.common.models.errors import LoginFailed
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.server.helpers.process import check_output
from music_assistant.server.providers.filesystem_local import (
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    LocalFileSystemProvider,
    exists,
    makedirs,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CONF_HOST = "host"
CONF_SHARE = "share"
CONF_SUBFOLDER = "subfolder"
CONF_MOUNT_OPTIONS = "mount_options"


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
    return SMBFileSystemProvider(mass, manifest, config)


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
    return (
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
            required=True,
            default_value="guest",
            description="The username to authenticate to the remote server. "
            "For anynymous access you may want to try with the user `guest`.",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
            default_value=None,
            description="The username to authenticate to the remote server. "
            "For anynymous access you may want to try with the user `guest`.",
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
            key=CONF_MOUNT_OPTIONS,
            type=ConfigEntryType.STRING,
            label="Mount options",
            required=False,
            category="advanced",
            default_value="noserverino,file_mode=0775,dir_mode=0775,uid=0,gid=0",
            description="[optional] Any additional mount options you "
            "want to pass to the mount command if needed for your particular setup.",
        ),
        CONF_ENTRY_MISSING_ALBUM_ARTIST,
    )


class SMBFileSystemProvider(LocalFileSystemProvider):
    """
    Implementation of an SMB File System Provider.

    Basically this is just a wrapper around the regular local files provider,
    except for the fact that it will mount a remote folder to a temporary location.
    We went for this OS-depdendent approach because there is no solid async-compatible
    smb library for Python (and we tried both pysmb and smbprotocol).
    """

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # base_path will be the path where we're going to mount the remote share
        self.base_path = f"/tmp/{self.instance_id}"  # noqa: S108
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

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        await self.unmount()

    async def mount(self) -> None:
        """Mount the SMB location to a temporary folder."""
        server = str(self.config.get_value(CONF_HOST))
        username = str(self.config.get_value(CONF_USERNAME))
        password = self.config.get_value(CONF_PASSWORD)
        share = str(self.config.get_value(CONF_SHARE))

        # handle optional subfolder
        subfolder = str(self.config.get_value(CONF_SUBFOLDER))
        if subfolder:
            subfolder = subfolder.replace("\\", "/")
            if not subfolder.startswith("/"):
                subfolder = "/" + subfolder
            if subfolder.endswith("/"):
                subfolder = subfolder[:-1]

        if platform.system() == "Darwin":
            # NOTE: MacOS does not support special characters in the username/password
            password_str = f":{password}" if password else ""
            mount_cmd = [
                "mount",
                "-t",
                "smbfs",
                f"//{username}{password_str}@{server}/{share}{subfolder}",
                self.base_path,
            ]

        elif platform.system() == "Linux":
            options = ["rw"]
            if mount_options := str(self.config.get_value(CONF_MOUNT_OPTIONS)):
                options += mount_options.split(",")

            options_str = ",".join(options)
            mount_cmd = [
                "mount",
                "-t",
                "cifs",
                "-o",
                options_str,
                f"//{server}/{share}{subfolder}",
                self.base_path,
            ]

        else:
            msg = f"SMB provider is not supported on {platform.system()}"
            raise LoginFailed(msg)

        self.logger.info("Mounting //%s/%s%s to %s", server, share, subfolder, self.base_path)
        self.logger.debug(
            "Using mount command: %s",
            [m.replace(str(password), "########") if password else m for m in mount_cmd],
        )
        env_vars = {
            **os.environ,
            "USER": username,
        }
        if password:
            env_vars["PASSWD"] = str(password)

        returncode, output = await check_output(*mount_cmd, env=env_vars)
        if returncode != 0:
            msg = f"SMB mount failed with error: {output.decode()}"
            raise LoginFailed(msg)

    async def unmount(self, ignore_error: bool = False) -> None:
        """Unmount the remote share."""
        returncode, output = await check_output("umount", self.base_path)
        if returncode != 0 and not ignore_error:
            self.logger.warning("SMB unmount failed with error: %s", output.decode())
