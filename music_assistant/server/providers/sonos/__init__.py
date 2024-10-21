"""
Sonos Player provider for Music Assistant for speakers running the S2 firmware.

Based on the aiosonos library, which leverages the new websockets API of the Sonos S2 firmware.
https://github.com/music-assistant/aiosonos
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant.common.models.config_entries import ConfigEntry, ConfigValueType
from music_assistant.constants import VERBOSE_LOG_LEVEL

from .provider import SonosPlayerProvider

if TYPE_CHECKING:
    from music_assistant.common.models.config_entries import ProviderConfig
    from music_assistant.common.models.provider import ProviderManifest
    from music_assistant.server import MusicAssistant
    from music_assistant.server.models import ProviderInstanceType


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = SonosPlayerProvider(mass, manifest, config)
    # set-up aiosonos logging
    if prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
        logging.getLogger("aiosonos").setLevel(logging.DEBUG)
    else:
        logging.getLogger("aiosonos").setLevel(prov.logger.level + 10)
    return prov


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
    return ()
