"""
MSX Bridge Player Provider for Music Assistant.

Streams music to Smart TVs via the Media Station X (MSX) app.
Runs an embedded HTTP server that MSX connects to for library browsing,
playback control, and audio streaming.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .constants import CONF_ENABLE_GROUPING, DEFAULT_ENABLE_GROUPING
from .provider import MSXBridgeProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # read the stored value directly: the config's option entries are only resolved
    # (and its values populated) once the instance exists, which is after this call
    grouping_enabled = bool(
        mass.config.get_raw_provider_config_value(
            config.instance_id, CONF_ENABLE_GROUPING, DEFAULT_ENABLE_GROUPING
        )
    )
    features: set[ProviderFeature] = {ProviderFeature.REMOVE_PLAYER}
    if grouping_enabled:
        features.add(ProviderFeature.SYNC_PLAYERS)
    return MSXBridgeProvider(mass, manifest, config, features)
