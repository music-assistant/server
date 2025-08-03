"""Constants for the AirPlay provider."""

from __future__ import annotations

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import DEFAULT_PCM_FORMAT

DOMAIN = "airplay"

CONF_ENCRYPTION = "encryption"
CONF_ALAC_ENCODE = "alac_encode"
CONF_VOLUME_START = "volume_start"
CONF_PASSWORD = "password"
CONF_READ_AHEAD_BUFFER = "read_ahead_buffer"
CONF_IGNORE_VOLUME = "ignore_volume"

BACKOFF_TIME_LOWER_LIMIT = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT = 300  # Five minutes

CONF_CREDENTIALS = "credentials"
CACHE_KEY_PREV_VOLUME = "airplay_prev_volume"
FALLBACK_VOLUME = 20

AIRPLAY_FLOW_PCM_FORMAT = AudioFormat(
    content_type=DEFAULT_PCM_FORMAT.content_type,
    sample_rate=44100,
    bit_depth=DEFAULT_PCM_FORMAT.bit_depth,
)
AIRPLAY_PCM_FORMAT = AudioFormat(
    content_type=ContentType.from_bit_depth(16), sample_rate=44100, bit_depth=16
)

BROKEN_RAOP_MODELS = (
    # A recent fw update of newer gen Sonos speakers block RAOP (airplay 1) support,
    # basically rendering our airplay implementation useless on these devices.
    # This list contains the models that are known to have this issue.
    # Hopefully the issue won't spread to other models.
    ("Sonos", "Era 100"),
    ("Sonos", "Era 300"),
    ("Sonos", "Move 2"),
    ("Sonos", "Roam 2"),
    ("Sonos", "Arc Ultra"),
    # Samsung has been repeatedly being reported as having issues with AirPlay 1/raop
    ("Samsung", "*"),
)
