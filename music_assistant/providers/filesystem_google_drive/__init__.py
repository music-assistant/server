"""Google Drive filesystem provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed

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

from .auth import authorize
from .constants import (
    CONF_ACTION_AUTH,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FOLDER_ID,
    CONF_REFRESH_TOKEN,
)
from .provider import GoogleDriveFileSystemProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # mass calls handle_async_init after setup returns; calling it here too
    # would register the stream route twice
    return GoogleDriveFileSystemProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    :param mass: The MusicAssistant instance.
    :param instance_id: ID of an existing provider instance (None if new instance setup).
    :param action: Optional action key called from config entries UI.
    :param values: The (intermediate) raw values for config entries sent with the action.
    """
    if values is None:
        values = {}

    if action == CONF_ACTION_AUTH and values.get("session_id"):
        client_id = str(values.get(CONF_CLIENT_ID) or "")
        client_secret = str(values.get(CONF_CLIENT_SECRET) or "")
        # the frontend masks a previously stored secret; fetch the real value
        if client_secret == SECURE_STRING_SUBSTITUTE and instance_id:
            client_secret = str(
                mass.config.get_raw_provider_config_value(instance_id, CONF_CLIENT_SECRET) or ""
            )
        if not client_id or not client_secret:
            raise LoginFailed("Enter the Google OAuth Client ID and Client Secret first")
        values[CONF_REFRESH_TOKEN] = await authorize(
            mass, str(values["session_id"]), client_id, client_secret
        )

    base_entries = (
        ConfigEntry(
            key=CONF_CLIENT_ID,
            type=ConfigEntryType.STRING,
            required=True,
            value=values.get(CONF_CLIENT_ID),
        ),
        ConfigEntry(
            key=CONF_CLIENT_SECRET,
            type=ConfigEntryType.SECURE_STRING,
            required=True,
            value=values.get(CONF_CLIENT_SECRET),
        ),
        ConfigEntry(
            key=CONF_ACTION_AUTH,
            type=ConfigEntryType.ACTION,
            action=CONF_ACTION_AUTH,
        ),
        ConfigEntry(
            key=CONF_REFRESH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            required=True,
            # filled by the authorize action above; hidden from the user
            hidden=True,
            value=values.get(CONF_REFRESH_TOKEN),
        ),
        ConfigEntry(
            key=CONF_FOLDER_ID,
            type=ConfigEntryType.STRING,
            required=False,
            default_value="root",
        ),
        CONF_ENTRY_MISSING_ALBUM_ARTIST,
        CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_TRACKS,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
        CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
        CONF_ENTRY_PROPAGATE_GENRES,
    )

    # content type is only choosable at initial setup; read-only afterwards
    if instance_id is None:
        return (
            CONF_ENTRY_CONTENT_TYPE,
            *base_entries,
        )
    return (
        *base_entries,
        CONF_ENTRY_CONTENT_TYPE_READ_ONLY,
    )
