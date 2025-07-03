"""
Home Assistant PlayerProvider for Music Assistant.

Allows using media_player entities in HA to be used as players in MA.
Requires the Home Assistant Plugin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.hass import DOMAIN as HASS_DOMAIN
from music_assistant.providers.hass.constants import MediaPlayerEntityFeature

from .constants import BLOCKLISTED_HASS_INTEGRATIONS, CONF_PLAYERS
from .provider import HomeAssistantPlayerProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from hass_client.models import CompressedState, EntityStateEvent
    from hass_client.models import Device as HassDevice
    from hass_client.models import Entity as HassEntity
    from hass_client.models import State as HassState
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.hass import HomeAssistantProvider


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    hass_prov: HomeAssistantProvider = mass.get_provider(HASS_DOMAIN)
    if not hass_prov:
        msg = "The Home Assistant Plugin needs to be set-up first"
        raise SetupFailedError(msg)
    prov = HomeAssistantPlayerProvider(mass, manifest, config)
    prov.hass_prov = hass_prov
    return prov


async def get_config_entries(
    mass: MusicAssistant,
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
    hass_prov: HomeAssistantProvider = mass.get_provider(HASS_DOMAIN)
    player_entities: list[ConfigValueOption] = []
    if hass_prov and hass_prov.hass.connected:
        async for state in _get_hass_media_players(hass_prov):
            name = f"{state['attributes']['friendly_name']} ({state['entity_id']})"
            player_entities.append(ConfigValueOption(name, state["entity_id"]))
    return (
        ConfigEntry(
            key=CONF_PLAYERS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Player entities",
            required=True,
            options=player_entities,
            description="Specify which HA media_player entity id's you "
            "like to import as players in Music Assistant.\n\n"
            "Note that only Media player entities will be listed which are "
            "compatible with Music Assistant.",
        ),
    )
