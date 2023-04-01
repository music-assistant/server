"""SMB filesystem provider for Music Assistant."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from music_assistant.common.helpers.util import get_ip_from_host
from music_assistant.common.models.config_entries import ConfigEntry
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.common.models.errors import LoginFailed
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.server.providers.filesystem_local.base import (
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    FileSystemProviderBase,
)

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType

CONF_HOST = "host"
CONF_SHARE = "share"
CONF_SUBFOLDER = "subfolder"
CONF_CONN_LIMIT = "connection_limit"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # silence logging a bit on smbprotocol
    logging.getLogger("smbprotocol").setLevel("WARNING")
    logging.getLogger("smbclient").setLevel("INFO")
    # check if valid dns name is given
    server: str = config.get_value(CONF_HOST)
    if not await get_ip_from_host(server):
        raise LoginFailed(f"Unable to resolve {server}, make sure the address is resolveable.")
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


class SMBFileSystemProvider(FileSystemProviderBase):
    """Implementation of an SMB File System Provider."""

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.server: str = self.config.get_value(CONF_HOST)
        self.share: str = self.config.get_value(CONF_SHARE)
        subfolder: str = self.config.get_value(CONF_SUBFOLDER)

        # create windows like path (\\server\share\subfolder)
        if subfolder.endswith(os.sep):
            subfolder = subfolder[:-1]
        self.subfolder = subfolder.replace("\\", os.sep).replace("/", os.sep)
        self._root_path = f"{os.sep}{os.sep}{self.server}{os.sep}{self.share}{os.sep}{subfolder}"
        self.logger.debug("Using root path: %s", self._root_path)

        # register smb session
        self.logger.info("Connecting to server %s", self.server)
        try:
            await self.mount_smb_share()
        except Exception as err:
            raise LoginFailed(f"Connection failed for the given details: {err}") from err

    async def mount_smb_share(self) -> None:
        """Mount the SMB share."""
        mount_point = f"/media/mass_smb/{self.instance_id}"
        # if not os.path.exists(mount_point):
        #     os.makedirs(mount_point)
        cmd = f"mount -t cifs {self._root_path} -o user={self.config.get_value(CONF_USERNAME)},password={self.config.get_value(CONF_PASSWORD)} {mount_point}"
        await run(cmd)


async def run(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await proc.communicate()

    print(f"[{cmd!r} exited with {proc.returncode}]")
    if stdout:
        print(f"[stdout]\n{stdout.decode()}")
    if stderr:
        print(f"[stderr]\n{stderr.decode()}")
