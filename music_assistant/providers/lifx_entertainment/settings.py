"""Readers for the LIFX Lights Sync playback settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import (
    CONF_BRIGHTNESS,
    CONF_COLOR_MODE,
    CONF_LIGHT_LATENCY_MS,
    CONF_POWER_ON,
    CONF_TRANSITION_MS,
    DEFAULT_BRIGHTNESS,
    DEFAULT_COLOR_MODE,
    DEFAULT_LIGHT_LATENCY_MS,
    DEFAULT_POWER_ON,
    DEFAULT_TRANSITION_MS,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig


def get_color_mode(config: ProviderConfig) -> str:
    """Return the configured visualization mode."""
    value = config.get_value(CONF_COLOR_MODE)
    return DEFAULT_COLOR_MODE if value is None else str(value)


def get_brightness(config: ProviderConfig) -> int:
    """Return the configured brightness percentage."""
    return _get_int(config, CONF_BRIGHTNESS, DEFAULT_BRIGHTNESS)


def get_light_latency_ms(config: ProviderConfig) -> int:
    """Return the configured latency compensation in milliseconds."""
    return _get_int(config, CONF_LIGHT_LATENCY_MS, DEFAULT_LIGHT_LATENCY_MS)


def get_transition_ms(config: ProviderConfig) -> int:
    """Return the configured per-frame transition time in milliseconds."""
    return _get_int(config, CONF_TRANSITION_MS, DEFAULT_TRANSITION_MS)


def get_power_on(config: ProviderConfig) -> bool:
    """Return whether lights that are off get switched on while music plays."""
    value = config.get_value(CONF_POWER_ON)
    return DEFAULT_POWER_ON if value is None else bool(value)


def _get_int(config: ProviderConfig, key: str, default: int) -> int:
    value = config.get_value(key)
    # stored numbers can come back as float or str, so normalize before truncating
    return default if value is None else int(float(str(value)))
