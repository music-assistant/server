"""Readers for the Hue Lights Sync playback settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import (
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_HUE_LATENCY_MS,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_MODE,
    DEFAULT_HUE_LATENCY_MS,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


def get_color_mode(config: ProviderConfig) -> str:
    """
    Return the configured visualization mode.

    :param config: Provider config to read the setting from.
    """
    value = config.get_value(CONF_COLOR_MODE)
    return DEFAULT_COLOR_MODE if value is None else str(value)


def get_brightness(config: ProviderConfig) -> int:
    """
    Return the configured brightness percentage.

    :param config: Provider config to read the setting from.
    """
    value = config.get_value(CONF_BRIGHTNESS)
    # stored numbers can come back as float or str, so normalize before truncating
    return DEFAULT_BRIGHTNESS if value is None else int(float(str(value)))


def get_hue_latency_ms(config: ProviderConfig) -> int:
    """
    Return the configured latency compensation in milliseconds.

    :param config: Provider config to read the setting from.
    """
    value = config.get_value(CONF_HUE_LATENCY_MS)
    return DEFAULT_HUE_LATENCY_MS if value is None else int(float(str(value)))
