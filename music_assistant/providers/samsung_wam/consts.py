"""Global constants and configurations for the Samsung WAM provider."""

from music_assistant_models.config_entries import MULTI_VALUE_SPLITTER, ConfigEntry
from music_assistant_models.enums import PlayerFeature

from music_assistant.constants import CONF_ENTRY_HTTP_PROFILE, CONF_ENTRY_SAMPLE_RATES

# --- Global Provider Settings ---

MANUFACTURER_NAME = "Samsung"

COMMAND_RETRY_ATTEMPTS = 3
COMMAND_RETRY_BACKOFF = 1.0

PLAYER_FEATURES_BASE = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.PAUSE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.SELECT_SOURCE,
}

# --- Configuration Entries ---

CONF_ENTRY_SAMPLE_RATES_WAM = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_SAMPLE_RATES.to_dict(),
        "default_value": [
            f"44100{MULTI_VALUE_SPLITTER}16",
            f"44100{MULTI_VALUE_SPLITTER}24",
            f"48000{MULTI_VALUE_SPLITTER}16",
            f"48000{MULTI_VALUE_SPLITTER}24",
            f"88200{MULTI_VALUE_SPLITTER}16",
            f"88200{MULTI_VALUE_SPLITTER}24",
            f"96000{MULTI_VALUE_SPLITTER}16",
            f"96000{MULTI_VALUE_SPLITTER}24",
            f"176400{MULTI_VALUE_SPLITTER}16",
            f"176400{MULTI_VALUE_SPLITTER}24",
            f"192000{MULTI_VALUE_SPLITTER}16",
            f"192000{MULTI_VALUE_SPLITTER}24",
        ],
    }
)

CONF_ENTRY_HTTP_PROFILE_WAM = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "default_value": "forced_content_length"}
)
