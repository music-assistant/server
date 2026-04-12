"""Config constants for the Hue Lights Sync provider."""

from __future__ import annotations

from typing import Final

CONF_BRIDGE_HOST: Final[str] = "bridge_host"
CONF_BRIDGE_ID: Final[str] = "bridge_id"
CONF_ACTION_PAIR: Final[str] = "pair"
CONF_USERNAME: Final[str] = "hue_username"
CONF_CLIENTKEY: Final[str] = "hue_clientkey"
CONF_BRIGHTNESS: Final[str] = "brightness"
CONF_INTENSITY: Final[str] = "intensity"
CONF_COLOR_MODE: Final[str] = "color_mode"

HUE_MDNS_TYPE: Final[str] = "_hue._tcp.local."
