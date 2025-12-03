"""Constants for the AirPlay provider."""

from __future__ import annotations

from enum import Enum
from typing import Final

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import INTERNAL_PCM_FORMAT

DOMAIN = "airplay"


class StreamingProtocol(Enum):
    """AirPlay streaming protocol versions."""

    RAOP = 1  # AirPlay 1 (RAOP)
    AIRPLAY2 = 2  # AirPlay 2


CACHE_CATEGORY_PREV_VOLUME: Final[int] = 1

CONF_ENCRYPTION: Final[str] = "encryption"
CONF_ALAC_ENCODE: Final[str] = "alac_encode"
CONF_VOLUME_START: Final[str] = "volume_start"
CONF_PASSWORD: Final[str] = "password"
CONF_IGNORE_VOLUME: Final[str] = "ignore_volume"
CONF_CREDENTIALS: Final[str] = "credentials"
CONF_AIRPLAY_PROTOCOL: Final[str] = "airplay_protocol"

AIRPLAY_DISCOVERY_TYPE: Final[str] = "_airplay._tcp.local."
RAOP_DISCOVERY_TYPE: Final[str] = "_raop._tcp.local."
DACP_DISCOVERY_TYPE: Final[str] = "_dacp._tcp.local."

AIRPLAY_PRELOAD_SECONDS: Final[int] = (
    5  # Number of seconds (in PCM) to preload before throttling back
)
AIRPLAY_PROCESS_SPAWN_TIME_MS: Final[int] = (
    200  # Time in ms to allow AirPlay CLI processes to spawn and initialise
)
AIRPLAY_OUTPUT_BUFFER_DURATION_MS: Final[int] = (
    2000  # Read ahead buffer for cliraop. Output buffer duration for cliap2.
)
AIRPLAY2_MIN_LOG_LEVEL: Final[int] = 3  # Min loglevel to ensure stderr output contains what we need
AIRPLAY2_CONNECT_TIME_MS: Final[int] = 2500  # Time in ms to allow AirPlay2 device to connect
CONF_AP_CREDENTIALS: Final[str] = "ap_credentials"
CONF_MRP_CREDENTIALS: Final[str] = "mrp_credentials"
CONF_ACTION_START_PAIRING: Final[str] = "start_ap_pairing"
CONF_ACTION_FINISH_PAIRING: Final[str] = "finish_ap_pairing"
CONF_ACTION_START_MRP_PAIRING: Final[str] = "start_mrp_pairing"
CONF_ACTION_FINISH_MRP_PAIRING: Final[str] = "finish_mrp_pairing"
CONF_PAIRING_PIN: Final[str] = "pairing_pin"
CONF_MRP_PAIRING_PIN: Final[str] = "mrp_pairing_pin"
CONF_ENABLE_LATE_JOIN: Final[str] = "enable_late_join"

BACKOFF_TIME_LOWER_LIMIT: Final[int] = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT: Final[int] = 300  # Five minutes
ENABLE_LATE_JOIN_DEFAULT: Final[bool] = True

FALLBACK_VOLUME: Final[int] = 20

AIRPLAY_FLOW_PCM_FORMAT = AudioFormat(
    content_type=INTERNAL_PCM_FORMAT.content_type,
    sample_rate=44100,
    bit_depth=INTERNAL_PCM_FORMAT.bit_depth,
)
AIRPLAY_PCM_FORMAT = AudioFormat(
    content_type=ContentType.from_bit_depth(16), sample_rate=44100, bit_depth=16
)

BROKEN_AIRPLAY_MODELS = (
    # A recent fw update of newer gen Sonos speakers have AirPlay issues,
    # basically rendering our (both AP2 and RAOP) implementation useless on these devices.
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

AIRPLAY_2_DEFAULT_MODELS = (
    # Models that are known to work better with AirPlay 2 protocol instead of RAOP
    ("Ubiquiti Inc.", "*"),
    ("Juke Audio", "*"),
)
