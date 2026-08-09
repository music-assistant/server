"""Configuration entries for the AI Radio plugin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import ConfigEntryType

from music_assistant.helpers.countries import get_country_codes
from music_assistant.helpers.datetime import host_timezone_name

from .constants import (
    CONF_TIMEZONE,
    CONF_WEATHER_CITY,
    CONF_WEATHER_COUNTRY,
    CONF_WEATHER_PROVIDER,
    CONF_WEATHER_TIMEOUT,
    DEFAULT_WEATHER_PROVIDER,
    DEFAULT_WEATHER_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    country_codes = await asyncio.to_thread(get_country_codes)
    country_options = [
        ConfigValueOption(title=name, value=code) for code, name in country_codes.items()
    ]
    # default the weather country to the region of the server's language, mirroring
    # the itunes_podcasts locale precedent
    region = mass.metadata.locale.split("_")[-1].upper()
    return (
        ConfigEntry(
            key=CONF_TIMEZONE,
            type=ConfigEntryType.STRING,
            default_value=host_timezone_name(),
            advanced=True,
        ),
        ConfigEntry(
            key=CONF_WEATHER_CITY,
            type=ConfigEntryType.STRING,
            default_value="",
        ),
        ConfigEntry(
            key=CONF_WEATHER_COUNTRY,
            type=ConfigEntryType.STRING,
            options=country_options,
            default_value=region if region in country_codes else "",
        ),
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
    )
