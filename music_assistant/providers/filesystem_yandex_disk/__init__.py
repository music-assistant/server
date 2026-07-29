"""Yandex Disk filesystem provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import LoginFailed
from ya_passport_auth.ma import DevicePageConfig, run_oauth_device_flow

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

from .constants import (
    CONF_ACTION_AUTH,
    CONF_AUTH_STATUS_CONNECTED,
    CONF_AUTH_STATUS_NOT_CONNECTED,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONF_ROOT_PATH,
    DISK_ROOT,
    OAUTH_SCOPE,
)
from .provider import YandexDiskFileSystemProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


_DEVICE_PAGE = DevicePageConfig(
    domain="filesystem_yandex_disk",
    title={
        "en": "Authorize Yandex Disk",
        "ru": "Авторизация Яндекс Диска",
    },
    context_text={
        "en": "Confirm read-only access for the OAuth application you configured.",
        "ru": "Подтвердите доступ только для чтения для настроенного OAuth-приложения.",
    },
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """
    Initialize a provider instance from its configuration.

    :param mass: The MusicAssistant instance.
    :param manifest: The provider manifest.
    :param config: The provider (instance) configuration.
    :returns: The constructed provider instance.
    """
    # MA calls handle_async_init after setup returns; calling it here too would
    # register the stream route twice.
    return YandexDiskFileSystemProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return the config entries for this provider.

    :param mass: The MusicAssistant instance.
    :param instance_id: Existing instance id (None on first setup).
    :param action: Optional action key from the config UI.
    :param values: Intermediate raw config values sent with the action.
    :returns: The ordered config entries.
    """
    if values is None:
        values = {}

    await _handle_auth_action(mass, instance_id, action, values)

    authenticated = bool(values.get(CONF_REFRESH_TOKEN))
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
            key=(CONF_AUTH_STATUS_CONNECTED if authenticated else CONF_AUTH_STATUS_NOT_CONNECTED),
            type=ConfigEntryType.LABEL,
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
            # filled by the Device Flow action above; hidden from the user
            hidden=True,
            value=values.get(CONF_REFRESH_TOKEN),
        ),
        ConfigEntry(
            key=CONF_ROOT_PATH,
            type=ConfigEntryType.STRING,
            required=False,
            default_value=DISK_ROOT,
            value=values.get(CONF_ROOT_PATH),
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
        return (CONF_ENTRY_CONTENT_TYPE, *base_entries)
    return (*base_entries, CONF_ENTRY_CONTENT_TYPE_READ_ONLY)


async def _handle_auth_action(
    mass: MusicAssistant,
    instance_id: str | None,
    action: str | None,
    values: dict[str, ConfigValueType],
) -> None:
    """
    Run Device Flow and write the resulting refresh token in place.

    :param mass: The MusicAssistant instance.
    :param instance_id: Existing instance id (for re-fetching a masked secret).
    :param action: The action key from the config UI.
    :param values: The config-flow values (mutated).
    """
    if action != CONF_ACTION_AUTH:
        return
    client_id = str(values.get(CONF_CLIENT_ID) or "")
    client_secret = str(values.get(CONF_CLIENT_SECRET) or "")
    # the frontend masks a previously stored secret; fetch and decrypt the real value
    if client_secret == SECURE_STRING_SUBSTITUTE and instance_id:
        client_secret = mass.config.decrypt_string(
            str(mass.config.get_raw_provider_config_value(instance_id, CONF_CLIENT_SECRET) or "")
        )
    if not client_id or not client_secret:
        raise LoginFailed("Enter the Yandex OAuth Client ID and Client Secret first")

    session_id = str(values.get("session_id") or "")
    if not session_id:
        raise LoginFailed("Authentication session is missing; close the dialog and try again")

    tokens = await run_oauth_device_flow(
        mass,
        session_id,
        _DEVICE_PAGE,
        client_id=client_id,
        client_secret=client_secret,
        scope=OAUTH_SCOPE,
        device_name="Music Assistant - Yandex Disk",
    )
    values[CONF_REFRESH_TOKEN] = tokens.refresh_token.get_secret()
