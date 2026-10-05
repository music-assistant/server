"""
Home Assistant PlayerProvider for Music Assistant.

Allows using media_player entities in HA to be used as players in MA.
Requires the Home Assistant Plugin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.hass import DOMAIN as HASS_DOMAIN

from .provider import HomeAssistantPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.hass import HomeAssistantProvider


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    hass_prov = mass.get_provider(HASS_DOMAIN)
    if not hass_prov:
        msg = "The Home Assistant Plugin needs to be set-up first"
        raise SetupFailedError(msg)
    hass_prov = cast("HomeAssistantProvider", hass_prov)
    return HomeAssistantPlayerProvider(mass, manifest, config, hass_prov)
