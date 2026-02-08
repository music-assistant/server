"""
MSX Bridge Player Provider for Music Assistant.

Streams music to Smart TVs via the Media Station X (MSX) app.
Runs an embedded HTTP server that MSX connects to for library browsing,
playback control, and audio streaming.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .constants import (
    CONF_HTTP_PORT,
    CONF_OUTPUT_FORMAT,
    DEFAULT_HTTP_PORT,
    DEFAULT_OUTPUT_FORMAT,
)
from .provider import MSXBridgeProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return MSXBridgeProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_HTTP_PORT,
            type=ConfigEntryType.INTEGER,
            label="HTTP Server Port",
            required=True,
            default_value=str(DEFAULT_HTTP_PORT),
            description="Port for the MSX HTTP server.",
        ),
        ConfigEntry(
            key=CONF_OUTPUT_FORMAT,
            type=ConfigEntryType.STRING,
            label="Audio Output Format",
            required=True,
            default_value=DEFAULT_OUTPUT_FORMAT,
            description="Audio format for streaming to MSX (mp3, aac, or flac).",
        ),
    )
