"""Global constants and configurations for the Samsung WAM provider."""

from music_assistant_models.enums import PlayerFeature

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
