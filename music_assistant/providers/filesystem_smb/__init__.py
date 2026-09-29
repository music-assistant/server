"""SMB filesystem provider for Music Assistant."""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.helpers.json import SerializableType
from music_assistant.helpers.mount import build_cifs_mount_cmd, classify_mount_error, unmount
from music_assistant.helpers.process import check_output
from music_assistant.helpers.util import get_ip_from_host
from music_assistant.providers.filesystem_local import (
    LocalFileSystemProvider,
    ismount,
    makedirs,
)
from music_assistant.providers.filesystem_local.constants import (
    CONF_CONTENT_TYPE,
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    CONF_ENTRY_PROPAGATE_GENRES,
    content_type_config_entry,
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
        content_type = str(
            self.get_setup_value(CONF_CONTENT_TYPE, CONF_ENTRY_CONTENT_TYPE.default_value)
        )
        return (
            content_type_config_entry(content_type),
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
        await super().unload(is_removed)
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

        mount_cmd, env_vars = build_cifs_mount_cmd(
            platform.system(),
            server,
            f"{share}{subfolder}",
            self.base_path,
            username=username,
            password=password_str,
            version=str(self.get_setup_value(CONF_SMB_VERSION) or "") or None,
            cache_mode=str(self.config.get_value(CONF_CACHE_MODE) or "loose"),
        )

        # never the command itself: on macOS it carries the password
        self.logger.debug("Mounting //%s/%s%s to %s", server, share, subfolder, self.base_path)
        returncode, output = await check_output(*mount_cmd, env=env_vars)
        if returncode != 0:
            raise classify_mount_error("cifs", output.decode().strip())
