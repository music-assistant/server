"""Constants for the Linn / OpenHome Media Provider."""

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_ENTRY_FLOW_MODE

CALLBACK_URL: str = "/notify_ohm"

RADIO = "Radio"
PLAYLIST = "Playlist"
EXTERNAL = "External"

radio_option = ConfigValueOption(
    RADIO, title="Radio", description="Play media by streaming to the player's Radio source"
)
playlist_option = ConfigValueOption(
    PLAYLIST, title="Playlist", description="Play media by adding to the player's Playlist source"
)

select_device_options: list[ConfigValueOption] = [
    radio_option,
    playlist_option,
]
CONF_SELECT_DEVICE_SOURCE_KEY = "select_device_source"
CONF_SELECT_DEVICE_SOURCE = ConfigEntry(
    key=CONF_SELECT_DEVICE_SOURCE_KEY,
    type=ConfigEntryType.STRING,
    options=select_device_options,
    label="Select playback mode",
    advanced=True,
    description="Radio mode (if available) will not alter the player's Playlist but will not play gapless unless in queue flow mode."
    "\nPlaylist mode will play gapless but will add tracks to the player's Playlist.",
    requires_reload=True,
)

PLAYER_CONFIG_ENTRIES: list[ConfigEntry] = [
    ConfigEntry.from_dict({**CONF_ENTRY_FLOW_MODE.to_dict(), "default_value": False}),
    CONF_SELECT_DEVICE_SOURCE,
]
