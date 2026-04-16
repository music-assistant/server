"""
Dashie Kiosk Player provider for Music Assistant.

Plays audio directly on Dashie Kiosk tablets via their REST API.
Supports automatic discovery via the Home Assistant Plugin and the Dashie HA
integration, or manual configuration by IP address.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.providers.hass import DOMAIN as HASS_DOMAIN

from .constants import CONF_MANUAL_PLAYERS, CONF_PLAYERS, DASHIE_HA_DOMAIN
from .provider import DashieKioskProvider

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
    # Wait for the hass provider to become available (not guaranteed to be
    # loaded yet since depends_on is not set -- manual config works without HA).
    hass_prov: HomeAssistantProvider | None = None
    for _ in range(10):
        if (raw_prov := mass.get_provider(HASS_DOMAIN)) and raw_prov.available:
            hass_prov = cast("HomeAssistantProvider", raw_prov)
            break
        await asyncio.sleep(1)
    return DashieKioskProvider(mass, manifest, config, hass_prov)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    hass_prov = cast("HomeAssistantProvider|None", mass.get_provider(HASS_DOMAIN))
    player_entities: list[ConfigValueOption] = []
    if hass_prov and hass_prov.hass.connected:
        entity_registry = {x["entity_id"]: x for x in await hass_prov.hass.get_entity_registry()}
        for state in await hass_prov.hass.get_states():
            if not state["entity_id"].startswith("media_player"):
                continue
            if "friendly_name" not in state["attributes"]:
                continue
            # Only show entities from the Dashie HA integration
            entity_entry = entity_registry.get(state["entity_id"])
            if not entity_entry or entity_entry.get("platform") != DASHIE_HA_DOMAIN:
                continue
            name = f"{state['attributes']['friendly_name']} ({state['entity_id']})"
            player_entities.append(ConfigValueOption(name, state["entity_id"]))
    return (
        ConfigEntry(
            key=CONF_PLAYERS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Dashie Kiosk devices (via Home Assistant)",
            required=False,
            options=player_entities,
            description="Select Dashie Kiosk tablets discovered through the "
            "Dashie HA integration. Requires the Home Assistant Plugin.",
        ),
        ConfigEntry(
            key=CONF_MANUAL_PLAYERS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            label="Manual Dashie Kiosk addresses",
            required=False,
            description="Manually add Dashie Kiosk tablets by IP address and port "
            "(e.g. 192.168.1.100:2323). Use this if you don't have the "
            "Dashie HA integration installed.",
            advanced=True,
        ),
    )
