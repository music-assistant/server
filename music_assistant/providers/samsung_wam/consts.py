"""Global constants and configurations for the Samsung WAM provider."""

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import PlayerFeature

from music_assistant.constants import CONF_ENTRY_HTTP_PROFILE

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
    PlayerFeature.PLAY_ANNOUNCEMENT,
}

# --- Configuration Entries ---

CONF_ENTRY_HTTP_PROFILE_WAM = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "default_value": "forced_content_length"}
)
