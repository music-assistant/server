"""
Home Assistant PlayerProvider for Music Assistant.

Allows using media_player entities in HA to be used as players in MA.
Requires the Home Assistant Plugin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import SetupFailedError

from music_assistant.providers.hass import DOMAIN as HASS_DOMAIN

from .constants import (
    CONF_PLAYERS,
    DISABLED_REASON_NATIVE_DUPLICATE,
    DISABLED_REASON_NATIVE_INTEGRATION,
    NATIVE_SUPPORTED_HASS_INTEGRATIONS,
)
from .helpers import get_hass_media_players, native_player_macs, normalized_mac
from .provider import HomeAssistantPlayerProvider

if TYPE_CHECKING:
    from hass_client.models import Device as HassDevice
    from hass_client.models import Entity as HassEntity
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


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    hass_prov = cast("HomeAssistantProvider|None", mass.get_provider(HASS_DOMAIN))
    player_entities: list[ConfigValueOption] = []
    if hass_prov and hass_prov.hass.connected:
        # entities that are already imported must stay selectable,
        # so editing the provider config never drops them
        selected_players = _selected_players(mass, instance_id, values)
        device_registry = {x["id"]: x for x in await hass_prov.hass.get_device_registry()}
        native_macs = native_player_macs(mass)
        async for state, entity_registry_entry in get_hass_media_players(hass_prov):
            entity_id = state["entity_id"]
            name = f"{state['attributes']['friendly_name']} ({entity_id})"
            disabled_reason: str | None = None
            if entity_id not in selected_players:
                disabled_reason = _native_support_reason(
                    entity_registry_entry, device_registry, native_macs
                )
            player_entities.append(
                ConfigValueOption(
                    entity_id,
                    title=name,
                    disabled=disabled_reason is not None,
                    disabled_reason=disabled_reason,
                )
            )
    return (
        ConfigEntry(
            key=CONF_PLAYERS,
            type=ConfigEntryType.STRING,
            multi_value=True,
            required=True,
            options=player_entities,
        ),
    )


def _selected_players(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType] | None,
) -> set[str]:
    """Return the entity ids currently selected as players (stored and in-flight)."""
    selected: set[str] = set()
    if values and isinstance(players_value := values.get(CONF_PLAYERS), list):
        selected.update(cast("list[str]", players_value))
    if instance_id and isinstance(
        stored_players := mass.config.get_raw_provider_config_value(instance_id, CONF_PLAYERS),
        list,
    ):
        selected.update(cast("list[str]", stored_players))
    return selected


def _native_support_reason(
    entity_registry_entry: HassEntity | None,
    device_registry: dict[str, HassDevice],
    native_macs: set[str],
) -> str | None:
    """Return why the entity can not be imported (natively supported), or None if it can."""
    if entity_registry_entry is None:
        return None
    if entity_registry_entry["platform"] in NATIVE_SUPPORTED_HASS_INTEGRATIONS:
        return DISABLED_REASON_NATIVE_INTEGRATION
    device = device_registry.get(entity_registry_entry["device_id"] or "")
    if device is not None and any(
        normalized_mac(connection[1]) in native_macs
        for connection in device.get("connections", [])
        if len(connection) == 2 and connection[0] == "mac"
    ):
        return DISABLED_REASON_NATIVE_DUPLICATE
    return None
