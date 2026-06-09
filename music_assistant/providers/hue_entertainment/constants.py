"""Config constants for the Hue Lights Sync provider."""

from __future__ import annotations

from typing import Final

CONF_BRIDGE_HOST: Final[str] = "bridge_host"
CONF_BRIDGE_ID: Final[str] = "bridge_id"
CONF_ACTION_PAIR: Final[str] = "pair"
CONF_USERNAME: Final[str] = "hue_username"
CONF_CLIENTKEY: Final[str] = "hue_clientkey"
CONF_BRIGHTNESS: Final[str] = "brightness"
CONF_COLOR_MODE: Final[str] = "color_mode"
CONF_HUE_LATENCY_MS: Final[str] = "hue_latency_ms"

# Visualization modes; first entry is the default. Used to build the config
# options and to migrate away orphaned stored values from older versions.
COLOR_MODES: Final[tuple[str, ...]] = ("smooth", "ambient", "flashing", "energetic")
DEFAULT_COLOR_MODE: Final[str] = COLOR_MODES[0]

DEFAULT_HUE_LATENCY_MS: Final[int] = 20

HUE_MDNS_TYPE: Final[str] = "_hue._tcp.local."
