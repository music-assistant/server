"""Constants for the AirPlay provider."""

from __future__ import annotations

from typing import Final

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import INTERNAL_PCM_FORMAT

DOMAIN = "airplay"

CACHE_CATEGORY_PREV_VOLUME: Final[int] = 1

CONF_ENCRYPTION: Final[str] = "encryption"
CONF_ALAC_ENCODE: Final[str] = "alac_encode"
CONF_VOLUME_START: Final[str] = "volume_start"
CONF_PASSWORD: Final[str] = "password"
CONF_READ_AHEAD_BUFFER: Final[str] = "read_ahead_buffer"
CONF_IGNORE_VOLUME: Final[str] = "ignore_volume"
CONF_CREDENTIALS: Final[str] = "credentials"

BACKOFF_TIME_LOWER_LIMIT: Final[int] = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT: Final[int] = 300  # Five minutes

FALLBACK_VOLUME: Final[int] = 20

AIRPLAY_FLOW_PCM_FORMAT = AudioFormat(
    content_type=INTERNAL_PCM_FORMAT.content_type,
    sample_rate=44100,
    bit_depth=INTERNAL_PCM_FORMAT.bit_depth,
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
