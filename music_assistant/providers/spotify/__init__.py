"""Spotify music provider support for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from music_assistant_models.errors import LoginFailed

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER, CONF_PROVIDERS

from .constants import (
    CONF_CLIENT_ID,
    CONF_REFRESH_TOKEN_DEPRECATED,
    CONF_REFRESH_TOKEN_DEV,
    CONF_REFRESH_TOKEN_GLOBAL,
    CONF_SYNC_AUDIOBOOK_PROGRESS,
    CONF_SYNC_PODCAST_PROGRESS,
)
from .provider import SpotifyProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    ProviderFeature.PLAYLIST_CREATE,
    ProviderFeature.PLAYLIST_TRACKS_EDIT,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    Authentication is handled by the setup flow (see setup_flow.py); only the genuine
    options are configurable here.

    :param mass: The MusicAssistant instance.
    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    # audiobook progress sync is only offered where the account's region supports audiobooks
    audiobooks_supported = bool(
        instance_id
        and (prov_instance := mass.get_provider(instance_id))
        and getattr(prov_instance, "audiobooks_supported", False)
    )
    return (
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        ConfigEntry(
            key=CONF_SYNC_PODCAST_PROGRESS,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            category="sync_options",
        ),
        ConfigEntry(
            key=CONF_SYNC_AUDIOBOOK_PROGRESS,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            category="sync_options",
            hidden=not audiobooks_supported,
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    _migrate_legacy_token(mass, config)
    # a global refresh token lives in setup_data (new installs) or, for installs configured
    # before setup flows existed, in the legacy config values (read-through covers both)
    if not (
        config.setup_data.get(CONF_REFRESH_TOKEN_GLOBAL)
        or config.get_value(CONF_REFRESH_TOKEN_GLOBAL)
    ):
        raise LoginFailed("Re-Authentication required")
    return SpotifyProvider(mass, manifest, config, SUPPORTED_FEATURES)


def _migrate_legacy_token(mass: MusicAssistant, config: ProviderConfig) -> None:
    """Move a legacy (pre global/dev split) refresh_token into setup_data, one-off."""
    legacy_token = config.get_value(CONF_REFRESH_TOKEN_DEPRECATED)
    if not legacy_token:
        return
    if config.setup_data.get(CONF_REFRESH_TOKEN_GLOBAL) or config.get_value(
        CONF_REFRESH_TOKEN_GLOBAL
    ):
        return
    # a custom client id means the legacy token authenticated a developer session
    custom_client_id = config.get_value(CONF_CLIENT_ID)
    target_key = CONF_REFRESH_TOKEN_DEV if custom_client_id else CONF_REFRESH_TOKEN_GLOBAL
    base = f"{CONF_PROVIDERS}/{config.instance_id}/setup_data"
    encrypted = mass.config.encrypt_string(str(legacy_token))
    mass.config.set(f"{base}/{target_key}", encrypted)
    config.setup_data[target_key] = encrypted
    if target_key == CONF_REFRESH_TOKEN_DEV and custom_client_id:
        client_id_encrypted = mass.config.encrypt_string(str(custom_client_id))
        mass.config.set(f"{base}/{CONF_CLIENT_ID}", client_id_encrypted)
        config.setup_data[CONF_CLIENT_ID] = client_id_encrypted
    # drop the deprecated legacy token now that it lives under its new key
    mass.config.set_raw_provider_config_value(
        config.instance_id, CONF_REFRESH_TOKEN_DEPRECATED, None
    )
