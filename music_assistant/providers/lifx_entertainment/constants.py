"""Config constants for the LIFX Lights Sync provider."""

from __future__ import annotations

from typing import Final

from music_assistant.providers.sendspin_lights.analyzer import COLOR_MODES as ANALYZER_COLOR_MODES

CONF_BROADCAST: Final[str] = "broadcast_address"
CONF_EXTRA_HOSTS: Final[str] = "extra_hosts"
CONF_DEVICES: Final[str] = "devices"
CONF_LIGHTS: Final[str] = "lights"
CONF_GROUPING: Final[str] = "grouping"
CONF_BRIGHTNESS: Final[str] = "brightness"
CONF_COLOR_MODE: Final[str] = "color_mode"
CONF_LIGHT_LATENCY_MS: Final[str] = "light_latency_ms"
CONF_TRANSITION_MS: Final[str] = "transition_ms"
CONF_POWER_ON: Final[str] = "power_on"

# First entry is the default.
COLOR_MODES: Final[tuple[str, ...]] = ANALYZER_COLOR_MODES
DEFAULT_COLOR_MODE: Final[str] = COLOR_MODES[0]

GROUPINGS: Final[tuple[str, ...]] = ("group", "all", "light")
DEFAULT_GROUPING: Final[str] = GROUPINGS[0]

DEFAULT_BROADCAST: Final[str] = "255.255.255.255"
DEFAULT_BRIGHTNESS: Final[int] = 100
# Wi-Fi round trip plus the per-frame transition.
DEFAULT_LIGHT_LATENCY_MS: Final[int] = 100
DEFAULT_TRANSITION_MS: Final[int] = 50
DEFAULT_POWER_ON: Final[bool] = True

DISCOVERY_TIMEOUT_S: Final[float] = 3.0
# Follows DHCP lease changes.
REDISCOVERY_INTERVAL_S: Final[float] = 600.0

# Spectrum requested from the Sendspin visualizer (same as the Hue plugin).
SPECTRUM_BINS: Final[int] = 17
SPECTRUM_SCALE: Final = "mel"
SPECTRUM_F_MIN: Final[int] = 20
SPECTRUM_F_MAX: Final[int] = 20000
