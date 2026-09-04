"""
MSX Bridge Player Provider for Music Assistant.

Streams music to Smart TVs via the Media Station X (MSX) app.
Runs an embedded HTTP server that MSX connects to for library browsing,
playback control, and audio streaming.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.enums import ProviderFeature

from .constants import LEGACY_CONF_ENABLE_GROUPING
from .provider import MSXBridgeProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

logger = logging.getLogger(__name__)
CONFIG_MIGRATION_ERRORS = (KeyError, OSError, RuntimeError, TypeError, ValueError)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    try:
        if (
            mass.config.get_raw_provider_config_value(
                config.instance_id, LEGACY_CONF_ENABLE_GROUPING
            )
            is not None
        ):
            await mass.config.remove_provider_config_value(
                config.instance_id, LEGACY_CONF_ENABLE_GROUPING
            )
    except CONFIG_MIGRATION_ERRORS:
        logger.warning("Unable to remove legacy MSX grouping config", exc_info=True)
    return MSXBridgeProvider(mass, manifest, config, {ProviderFeature.REMOVE_PLAYER})
