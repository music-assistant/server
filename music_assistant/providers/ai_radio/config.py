"""Configuration entries for the AI Radio plugin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.helpers.countries import get_country_codes
from music_assistant.helpers.datetime import host_timezone_name

from .constants import (
    CONF_TIMEZONE,
    CONF_TTS_LOUDNESS_BOOST,
    CONF_WEATHER_CITY,
    CONF_WEATHER_COUNTRY,
    CONF_WEATHER_PROVIDER,
    CONF_WEATHER_TIMEOUT,
    DEFAULT_TTS_LOUDNESS_BOOST,
    DEFAULT_WEATHER_PROVIDER,
    DEFAULT_WEATHER_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    weather_city, weather_country = await get_weather_location_entries(mass, instance_id)
    return (
        ConfigEntry(
            key=CONF_TIMEZONE,
            type=ConfigEntryType.STRING,
            default_value=host_timezone_name(),
            advanced=True,
        ),
        weather_city,
        weather_country,
        ConfigEntry(
            key=CONF_WEATHER_PROVIDER,
            type=ConfigEntryType.STRING,
            options=[
                ConfigValueOption(value="open_meteo"),
                ConfigValueOption(value="disabled"),
            ],
            default_value=DEFAULT_WEATHER_PROVIDER,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_WEATHER_TIMEOUT,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_WEATHER_TIMEOUT_SECONDS,
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_TTS_LOUDNESS_BOOST,
            type=ConfigEntryType.INTEGER,
            default_value=DEFAULT_TTS_LOUDNESS_BOOST,
            range=(0, 6),
            advanced=True,
        ),
    )


async def get_weather_location_entries(
    mass: MusicAssistant, instance_id: str | None
) -> tuple[ConfigEntry, ConfigEntry]:
    """
    Build the weather city/country config entries, shared by the setup flow and options page.

    Defaults to the answer collected during setup when the instance already has one,
    so the weather location works right after setup without a trip to the settings
    screen, while still letting a later settings edit override it.

    :param mass: The MusicAssistant instance.
    :param instance_id: The provider instance id, or None during initial setup.
    """
    country_codes = await asyncio.to_thread(get_country_codes)
    country_options = [
        ConfigValueOption(title=name, value=code) for code, name in country_codes.items()
    ]
    # default the weather country to the region of the server's language, mirroring
    # the itunes_podcasts locale precedent
    region = mass.metadata.locale.split("_")[-1].upper()
    setup_city, setup_country = _setup_weather_location(mass, instance_id)
    return (
        ConfigEntry(
            key=CONF_WEATHER_CITY,
            type=ConfigEntryType.STRING,
            default_value=setup_city,
        ),
        ConfigEntry(
            key=CONF_WEATHER_COUNTRY,
            type=ConfigEntryType.STRING,
            options=country_options,
            default_value=setup_country or (region if region in country_codes else ""),
        ),
    )


def _setup_weather_location(mass: MusicAssistant, instance_id: str | None) -> tuple[str, str]:
    """Return the city/country collected by the setup flow, if any."""
    if instance_id is None:
        return "", ""
    setup_data = mass.config.get(f"{CONF_PROVIDERS}/{instance_id}/setup_data") or {}
    city = setup_data.get(CONF_WEATHER_CITY)
    country = setup_data.get(CONF_WEATHER_COUNTRY)
    if isinstance(city, str):
        city = mass.config.decrypt_string(city)
    if isinstance(country, str):
        country = mass.config.decrypt_string(country)
    return city or "", country or ""
