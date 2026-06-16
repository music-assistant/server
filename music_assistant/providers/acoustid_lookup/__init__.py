"""AcoustID Lookup audio analysis provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from .provider import (
    CONF_ANALYSE_STREAMING,
    CONF_API_KEY,
    CONF_MIN_SCORE,
    CONF_WRITE_TAGS_BACK,
    DEFAULT_MIN_SCORE,
    AcoustidLookupProvider,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> AcoustidLookupProvider:
    """Set up the AcoustID Lookup provider."""
    return AcoustidLookupProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return config entries for this provider."""
    return (
        ConfigEntry(
            key=CONF_API_KEY,
            type=ConfigEntryType.SECURE_STRING,
            required=False,
            default_value=None,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_MIN_SCORE,
            type=ConfigEntryType.FLOAT,
            default_value=DEFAULT_MIN_SCORE,
            range=(0, 1),
            required=False,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_ANALYSE_STREAMING,
            type=ConfigEntryType.BOOLEAN,
            default_value=True,
            required=False,
        ),
        ConfigEntry(
            key=CONF_WRITE_TAGS_BACK,
            type=ConfigEntryType.BOOLEAN,
            default_value=False,
            required=False,
        ),
    )
