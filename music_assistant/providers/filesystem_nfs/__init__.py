"""NFS filesystem provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.helpers.security import is_safe_path
from music_assistant.helpers.util import get_ip_from_host
from music_assistant.providers.filesystem_local.constants import (
    CONF_ENTRY_CONTENT_TYPE,
    CONF_ENTRY_CONTENT_TYPE_READ_ONLY,
    CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
    CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
    CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
    CONF_ENTRY_LIBRARY_SYNC_TRACKS,
    CONF_ENTRY_MISSING_ALBUM_ARTIST,
    CONF_ENTRY_PROPAGATE_GENRES,
)

from .constants import CONF_EXPORT_PATH, CONF_HOST, CONF_NFS_VERSION, CONF_SUBFOLDER
from .provider import NFSFileSystemProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # check if valid dns name is given for the host
    server = str(config.get_value(CONF_HOST))
    if not await get_ip_from_host(server):
        msg = f"Unable to resolve {server}, make sure the address is resolvable."
        raise SetupFailedError(msg)
    # check if export path is valid
    export_path = str(config.get_value(CONF_EXPORT_PATH))
    if not export_path or not export_path.startswith("/") or not is_safe_path(export_path):
        msg = "Invalid export path: must be an absolute path starting with /"
        raise SetupFailedError(msg)
    # base_path will be the path where we're going to mount the NFS export
    base_path = f"/tmp/{config.instance_id}"  # noqa: S108
    return NFSFileSystemProvider(mass, manifest, config, base_path)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param mass: The MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    base_entries = (
        ConfigEntry(
            key=CONF_HOST,
            type=ConfigEntryType.STRING,
            label="Server",
            required=True,
            description="The hostname or IP address of the NFS server to connect to. "
            "For example mynas.local or 192.168.1.100.",
        ),
        ConfigEntry(
            key=CONF_EXPORT_PATH,
            type=ConfigEntryType.STRING,
            label="Export path",
            required=True,
            description="The NFS export path on the remote server. "
            "For example /volume1/music or /exports/media.",
        ),
        ConfigEntry(
            key=CONF_SUBFOLDER,
            type=ConfigEntryType.STRING,
            label="Subfolder",
            required=False,
            default_value="",
            description="[optional] Use if your music is stored in a sublevel of the export. "
            "E.g. 'collections' or 'albums/A-K'.",
        ),
        ConfigEntry(
            key=CONF_NFS_VERSION,
            type=ConfigEntryType.STRING,
            label="NFS Version",
            required=False,
            advanced=True,
            default_value="",
            options=[
                ConfigValueOption("Auto", ""),
                ConfigValueOption("NFS 3", "3"),
                ConfigValueOption("NFS 4", "4"),
                ConfigValueOption("NFS 4.1", "4.1"),
                ConfigValueOption("NFS 4.2", "4.2"),
            ],
            description="The NFS protocol version to use. "
            "Use Auto to let the system negotiate the best available version.",
        ),
        CONF_ENTRY_MISSING_ALBUM_ARTIST,
        CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_TRACKS,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
        CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
        CONF_ENTRY_PROPAGATE_GENRES,
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
