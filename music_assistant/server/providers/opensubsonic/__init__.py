"""Open Subsonic music provider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.common.models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant.common.models.enums import ConfigEntryType
from music_assistant.constants import CONF_PASSWORD, CONF_PATH, CONF_PORT, CONF_USERNAME

from .sonic_provider import (
    CONF_BASE_URL,
    CONF_ENABLE_LEGACY_AUTH,
    CONF_ENABLE_PODCASTS,
    OpenSonicProvider,
)

if TYPE_CHECKING:
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = OpenSonicProvider(mass, manifest, config)
    await prov.handle_async_init()
    return prov


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Username",
            required=True,
            description="Your username for this Open Subsonic server",
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=True,
            description="The password associated with the username",
        ),
        ConfigEntry(
            key=CONF_BASE_URL,
            type=ConfigEntryType.STRING,
            label="Base URL",
            required=True,
            description="Base URL for the server, e.g. " "https://subsonic.mydomain.tld",
        ),
        ConfigEntry(
            key=CONF_PORT,
            type=ConfigEntryType.INTEGER,
            label="Port",
            required=False,
            description="Port Number for the server",
        ),
        ConfigEntry(
            key=CONF_PATH,
            type=ConfigEntryType.STRING,
            label="Server Path",
            required=False,
            description="Path to append to base URL for Soubsonic server, this is likely "
            "empty unless you are path routing on a proxy",
        ),
        ConfigEntry(
            key=CONF_ENABLE_PODCASTS,
            type=ConfigEntryType.BOOLEAN,
            label="Enable Podcasts",
            required=True,
            description="Should the provider query for podcasts as well as music?",
            default_value=True,
        ),
        ConfigEntry(
            key=CONF_ENABLE_LEGACY_AUTH,
            type=ConfigEntryType.BOOLEAN,
            label="Enable legacy auth",
            required=True,
            description='Enable OpenSubsonic "legacy" auth support',
            default_value=False,
        ),
    )
