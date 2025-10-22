"""
Sonos Player S1 provider for Music Assistant.

Based on the SoCo library for Sonos which uses the legacy/V1 UPnP API.

Note that large parts of this code are copied over from the Home Assistant
integration for Sonos.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import ConfigEntryType, ProviderFeature
from soco.discovery import scan_network

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS

from .constants import CONF_HOUSEHOLD_ID, CONF_NETWORK_SCAN
from .provider import SonosPlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from soco import SoCo

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SYNC_PLAYERS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SonosPlayerProvider(mass, manifest, config, SUPPORTED_FEATURES)


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
    household_ids = await discover_household_ids(mass)
    return (
        CONF_ENTRY_MANUAL_DISCOVERY_IPS,
        ConfigEntry(
            key=CONF_NETWORK_SCAN,
            type=ConfigEntryType.BOOLEAN,
            label="Enable network scan for discovery",
            default_value=False,
            description="Enable network scan for discovery of players. \n"
            "Can be used if (some of) your players are not automatically discovered.\n"
            "Should normally not be needed",
        ),
        ConfigEntry(
            key=CONF_HOUSEHOLD_ID,
            type=ConfigEntryType.STRING,
            label="Household ID",
            default_value=household_ids[0] if household_ids else None,
            description="Household ID for the Sonos (S1) system. Will be auto detected if empty.",
            category="advanced",
            required=False,
        ),
    )


async def discover_household_ids(mass: MusicAssistant, prefer_s1: bool = True) -> list[str]:
    """Discover the HouseHold ID of S1 speaker(s) the network."""
    if cache := await mass.cache.get("sonos_household_ids"):
        return cast("list[str]", cache)
    household_ids: list[str] = []

    def get_all_sonos_ips() -> set[SoCo]:
        """Run full network discovery and return IP's of all devices found on the network."""
        discovered_zones: set[SoCo] | None
        if discovered_zones := scan_network(multi_household=True):
            return {zone.ip_address for zone in discovered_zones}
        return set()

    all_sonos_ips = await asyncio.to_thread(get_all_sonos_ips)
    for ip_address in all_sonos_ips:
        async with mass.http_session.get(f"http://{ip_address}:1400/status/zp") as resp:
            if resp.status == 200:
                data = await resp.text()
                if prefer_s1 and "<SWGen>2</SWGen>" in data:
                    continue
                if "HouseholdControlID" in data:
                    household_id = data.split("<HouseholdControlID>")[1].split(
                        "</HouseholdControlID>"
                    )[0]
                    household_ids.append(household_id)
    await mass.cache.set("sonos_household_ids", household_ids, 3600)
    return household_ids
