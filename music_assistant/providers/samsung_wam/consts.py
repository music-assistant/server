"""Global constants and configurations for the Samsung WAM provider."""

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import PlayerFeature

from music_assistant.constants import CONF_ENTRY_HTTP_PROFILE, create_sample_rates_config_entry

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

CONF_ENTRY_SAMPLE_RATES_WAM = create_sample_rates_config_entry(
    supported_sample_rates=[44100, 48000, 88200, 96000, 176400, 192000],
    supported_bit_depths=[16, 24],
    safe_max_sample_rate=192000,
    safe_max_bit_depth=24,
)

CONF_ENTRY_HTTP_PROFILE_WAM = ConfigEntry.from_dict(
    {**CONF_ENTRY_HTTP_PROFILE.to_dict(), "default_value": "forced_content_length"}
)
