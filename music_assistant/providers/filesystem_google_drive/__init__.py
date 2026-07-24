"""Google Drive filesystem provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

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
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    Credentials, the content type and root folder are collected by the setup flow (see
    setup_flow.py); only the genuine sync options are configurable here.

    :param mass: The MusicAssistant instance.
    :param instance_id: ID of an existing provider instance (None if new instance setup).
    :param action: Optional action key called from config entries UI.
    :param values: The (intermediate) raw values for config entries sent with the action.
    """
    # the content type is set by the setup flow; surface it read-only so the sync
    # options' depends_on chains still resolve
    content_type = "music"
    if instance_id and (prov := mass.get_provider(instance_id, return_unavailable=True)):
        content_type = getattr(prov, "media_content_type", content_type)
    return (
        ConfigEntry(key=CONF_CONTENT_TYPE, type=ConfigEntryType.LABEL, value=content_type),
        CONF_ENTRY_MISSING_ALBUM_ARTIST,
        CONF_ENTRY_IGNORE_ALBUM_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_TRACKS,
        CONF_ENTRY_LIBRARY_SYNC_PLAYLISTS,
        CONF_ENTRY_LIBRARY_SYNC_PODCASTS,
        CONF_ENTRY_LIBRARY_SYNC_AUDIOBOOKS,
        CONF_ENTRY_PROPAGATE_GENRES,
    )
