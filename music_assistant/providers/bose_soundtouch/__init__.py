"""
Provider for Bose SoundTouch speakers.

Following the Bose SoundTouch end of life, this provider keeps the speakers usable
within Music Assistant: it detects them on the network, exposes native control
(power, volume, transport, source and multiroom grouping) and maps the physical
preset buttons to Music Assistant content. Audio playback is delegated to a linked
playback protocol (such as DLNA) via the standard protocol linking mechanism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS

from .provider import BoseSoundTouchProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SYNC_PLAYERS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return BoseSoundTouchProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    return (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)
