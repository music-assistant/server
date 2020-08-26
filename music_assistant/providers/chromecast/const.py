"""Constants for the implementation."""
from music_assistant.constants import CONF_ENABLED
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType

PROV_ID = "chromecast"
PROV_NAME = "Chromecast"

CONF_GAPLESS = "gapless_enabled"


PROVIDER_CONFIG_ENTRIES = [
    ConfigEntry(entry_key=CONF_ENABLED,
                entry_type=ConfigEntryType.BOOL,
                default_value=True, description_key=CONF_ENABLED)]

PLAYER_CONFIG_ENTRIES = [
    ConfigEntry(entry_key=CONF_GAPLESS,
                entry_type=ConfigEntryType.BOOL,
                default_value=True, description_key=CONF_GAPLESS)]
