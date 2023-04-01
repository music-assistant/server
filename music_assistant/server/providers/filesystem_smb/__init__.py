"""SMB filesystem provider for Music Assistant."""
from __future__ import annotations

import asyncio
import platform
from typing import TYPE_CHECKING

from music_assistant.common.helpers.util import get_ip_from_host
from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.common.models.errors import LoginFailed
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
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


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # check if valid dns name is given for the host
    server: str = config.get_value(CONF_HOST)
    if not await get_ip_from_host(server):
        raise LoginFailed(f"Unable to resolve {server}, make sure the address is resolveable.")
    # check if share is valid
    share: str = config.get_value(CONF_SHARE)
    if not share or "/" in share or "\\" in share:
        raise LoginFailed("Invalid share name")
    prov = SMBFileSystemProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant, manifest: ProviderManifest  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
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

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        # base_path will be the path where we're going to mount the remote share
        self.base_path = f"/tmp/{self.instance_id}"
        if not await exists(self.base_path):
            await makedirs(self.base_path)

        try:
            await self.mount()
        except Exception as err:
            raise LoginFailed(f"Connection failed for the given details: {err}") from err

    async def unload(self) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        """
        await self.unmount()

    async def mount(self) -> None:
        """Mount the SMB location to a temporary folder."""
        server: str = self.config.get_value(CONF_HOST)
        username: str = self.config.get_value(CONF_USERNAME)
        password: str = self.config.get_value(CONF_PASSWORD)
        share: str = self.config.get_value(CONF_SHARE)
        # handle optional subfolder
        subfolder: str = self.config.get_value(CONF_SUBFOLDER)
        if subfolder:
            subfolder = subfolder.replace("\\", "/")
            if not subfolder.startswith("/"):
                subfolder = "/" + subfolder
            if subfolder.endswith("/"):
                subfolder = subfolder[:-1]

        if platform.system() == "Darwin":
            password = f":{password}" if password else ""
            mount_cmd = f"mount -t smbfs //{username}{password}@{server}/{share}{subfolder} {self.base_path}"  # noqa: E501

        elif platform.system() == "Linux":
            password = f",password={password}" if password else ""
            mount_cmd = f"mount -t cifs //{server}/{share}{subfolder} -o user={username}{password} {self.base_path}"  # noqa: E501

        else:
            raise LoginFailed(f"SMB provider is not supported on {platform.system()}")

        self.logger.info("Mounting \\\\%s\\%s%s to %s", server, share, subfolder, self.base_path)
        self.logger.debug("Using mount command: %s", mount_cmd)

        proc = await asyncio.create_subprocess_shell(
            mount_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise LoginFailed("SMB mount failed with error: %s", stderr.decode())

    async def unmount(self) -> None:
        """Unmount the remote share."""
        proc = await asyncio.create_subprocess_shell(
            f"umount {self.base_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise LoginFailed("SMB unmount failed with error: %s", stderr.decode())
