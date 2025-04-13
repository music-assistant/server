"""AirPlay Player provider for Music Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.provider import ProviderManifest

from music_assistant.mass import MusicAssistant

from .const import CONF_BIND_INTERFACE
from .provider import AirPlayProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models import ProviderInstanceType


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_BIND_INTERFACE,
            type=ConfigEntryType.STRING,
            default_value=cast("str", mass.streams.publish_ip),
            label="Bind interface",
            description="Interface to bind to for AirPlay streaming.",
            category="advanced",
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AirPlayProvider(mass, manifest, config)
