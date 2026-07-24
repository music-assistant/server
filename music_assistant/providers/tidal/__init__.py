"""Tidal music provider support for MusicAssistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER

from .constants import CONF_QUALITY
from .provider import TidalProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return TidalProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return the configuration (options) entries for the Tidal provider.

    Authentication runs in the interactive setup flow (see ``setup_flow.py``); the only
    genuine option configured here is the preferred streaming quality.

    :param mass: The MusicAssistant instance.
    :param instance_id: Optional existing provider instance id (unused).
    :param action: Unused; retained for the config-entries signature contract.
    :param values: Unused; retained for the config-entries signature contract.
    """
    return (
        CONF_ENTRY_UNOFFICIAL_PROVIDER,
        ConfigEntry(
            key=CONF_QUALITY,
            type=ConfigEntryType.STRING,
            required=True,
            options=[
                ConfigValueOption("LOSSLESS"),
                ConfigValueOption("HI_RES_LOSSLESS"),
            ],
            default_value="HI_RES_LOSSLESS",
        ),
    )
