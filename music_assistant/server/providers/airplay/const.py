"""Constants for the AirPlay provider."""

from __future__ import annotations

from music_assistant.common.models.enums import ContentType
from music_assistant.common.models.media_items import AudioFormat

DOMAIN = "airplay"

CONF_ENCRYPTION = "encryption"
CONF_ALAC_ENCODE = "alac_encode"
CONF_VOLUME_START = "volume_start"
CONF_PASSWORD = "password"
CONF_BIND_INTERFACE = "bind_interface"
CONF_READ_AHEAD_BUFFER = "read_ahead_buffer"

BACKOFF_TIME_LOWER_LIMIT = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT = 300  # Five minutes

CONF_CREDENTIALS = "credentials"
CACHE_KEY_PREV_VOLUME = "airplay_prev_volume"
FALLBACK_VOLUME = 20

AIRPLAY_FLOW_PCM_FORMAT = AudioFormat(
    content_type=ContentType.PCM_F32LE,
    sample_rate=44100,
    bit_depth=32,
)
AIRPLAY_PCM_FORMAT = AudioFormat(
    content_type=ContentType.from_bit_depth(16), sample_rate=44100, bit_depth=16
)
