"""Constants for the Home Assistant PlayerProvider."""

from __future__ import annotations

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

CONF_PLAYERS = "players"

BLOCKLISTED_HASS_INTEGRATIONS = ("alexa_media", "apple_tv")
WARN_HASS_INTEGRATIONS = ("cast", "dlna_dmr", "fully_kiosk", "sonos", "snapcast")


CONF_ENTRY_WARN_HASS_INTEGRATION = ConfigEntry(
    key="warn_hass_integration",
    type=ConfigEntryType.ALERT,
    label="Music Assistant has native support for this player type - "
    "it is strongly recommended to use the native player provider for this player in "
    "Music Assistant instead of the generic version provided by the Home Assistant provider.",
)
