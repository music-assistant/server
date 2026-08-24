"""Constants for the Home Assistant PlayerProvider."""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

DOMAIN = "hass_players"

CONF_PLAYERS = "players"

# HA integrations that are known to not work (correctly) at all through this provider.
BLOCKLISTED_HASS_INTEGRATIONS = ("alexa_media", "apple_tv")
# HA integrations whose players are fully supported by a native Music Assistant
# player provider: importing them here is blocked (use the native provider instead).
NATIVE_SUPPORTED_HASS_INTEGRATIONS = (
    "bluesound",
    "cast",
    "dlna_dmr",
    "fully_kiosk",
    "heos",
    "linkplay",
    "mpd",
    "snapcast",
    "sonos",
    "squeezebox",
    "yamaha_musiccast",
)

DISABLED_REASON_NATIVE_INTEGRATION = (
    "Music Assistant has native support for this player type - "
    "set up the native player provider instead."
)
DISABLED_REASON_NATIVE_DUPLICATE = (
    "This device is already available in Music Assistant as a native player."
)

CONF_ENTRY_WARN_HASS_INTEGRATION = ConfigEntry(
    key="warn_hass_integration",
    type=ConfigEntryType.ALERT,
)
