"""Constants for the MPD Player Provider."""

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import PlaybackState

from music_assistant.constants import CONF_ENTRY_OUTPUT_CODEC

RECONNECT_DELAY = 5  # seconds between reconnect attempts
ELAPSED_POLL_INTERVAL = 5  # seconds between elapsed time polls while playing
HEALTHCHECK_POLL_INTERVAL = 30  # seconds between liveness polls while the player is idle
COMMAND_TIMEOUT = 10  # seconds before an unanswered MPD command counts as a lost connection
CONNECT_TIMEOUT = 10  # seconds before a connect attempt is given up and retried

# FLAC does not work for infinite HTTP streams - MPD cannot probe the header.
# Offer MP3, AAC, WAV only, with MP3 as default.
# from_dict expects options as plain dicts, not ConfigValueOption objects.
CONF_ENTRY_OUTPUT_CODEC_MPD = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_OUTPUT_CODEC.to_dict(),
        "default_value": "mp3",
        "options": [
            {"title": "MP3 (lossy)", "value": "mp3"},
            {"title": "AAC (lossy)", "value": "aac"},
            {"title": "WAV (uncompressed PCM)", "value": "wav"},
        ],
    }
)

MPD_STATE_MAP: dict[str, PlaybackState] = {
    "play": PlaybackState.PLAYING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.IDLE,
}
