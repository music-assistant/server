"""Constants for the AirPlay provider."""

from __future__ import annotations

from enum import IntEnum
from typing import Final

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ContentType, PlayerFeature
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import INTERNAL_PCM_FORMAT

DOMAIN = "airplay"


class StreamingProtocol(IntEnum):
    """AirPlay streaming protocol versions."""

    RAOP = 1  # AirPlay 1 (RAOP)
    AIRPLAY2 = 2  # AirPlay 2


CONF_ENCRYPTION: Final[str] = "encryption"
CONF_ALAC_ENCODE: Final[str] = "alac_encode"
CONF_VOLUME_START: Final[str] = "volume_start"
CONF_PASSWORD: Final[str] = "password"
CONF_IGNORE_VOLUME: Final[str] = "ignore_volume"
CONF_CREDENTIALS: Final[str] = "credentials"
CONF_AIRPLAY_PROTOCOL: Final[str] = "airplay_protocol"
CONF_STORED_VOLUME: Final[str] = "stored_volume"

AIRPLAY_DISCOVERY_TYPE: Final[str] = "_airplay._tcp.local."
RAOP_DISCOVERY_TYPE: Final[str] = "_raop._tcp.local."
DACP_DISCOVERY_TYPE: Final[str] = "_dacp._tcp.local."

# Read ahead buffer for cliraop. Default output buffer duration.
AIRPLAY_OUTPUT_BUFFER_DEFAULT_DURATION_MS: Final[int] = 1000
# Minimum output buffer duration permitted.
AIRPLAY_OUTPUT_BUFFER_MIN_DURATION_MS: Final[int] = 500
# Maximum output buffer duration permitted.
AIRPLAY_OUTPUT_BUFFER_MAX_DURATION_MS: Final[int] = 5000
AIRPLAY2_MIN_LOG_LEVEL: Final[int] = 3  # Min loglevel to ensure stderr output contains what we need
AIRPLAY2_CONNECT_TIME_MS: Final[int] = 1500  # Time in ms to allow AirPlay2 device to connect
RAOP_CONNECT_TIME_MS: Final[int] = 1000  # Time in ms to allow RAOP device to connect

# Per-protocol credential storage keys
CONF_RAOP_CREDENTIALS: Final[str] = "raop_credentials"
CONF_AIRPLAY_CREDENTIALS: Final[str] = "airplay_credentials"
CONF_AIRPLAY_LATENCY: Final[str] = "airplay_latency"

# Legacy credential key (for migration)
CONF_AP_CREDENTIALS: Final[str] = "ap_credentials"

# Pairing action keys
CONF_ACTION_START_PAIRING: Final[str] = "start_pairing"
CONF_ACTION_FINISH_PAIRING: Final[str] = "finish_pairing"
CONF_ACTION_RESET_PAIRING: Final[str] = "reset_pairing"
CONF_PAIRING_PIN: Final[str] = "pairing_pin"
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
    # Samsung has been repeatedly being reported as having issues with AirPlay (raop and AP2)
    ("Samsung", "*"),
)

AIRPLAY_2_DEFAULT_MODELS = (
    # Models that are known to work better with AirPlay 2 protocol instead of RAOP
    # These use the translated/friendly model names from get_model_info()
    ("Ubiquiti Inc.", "*"),
    ("LG Electronics", "*"),
)

PIN_REQUIRED = 0x8
LEGACY_PAIRING_BIT = 0x200
BROKEN_AIRPLAY_WARN = ConfigEntry(
    key="BROKEN_AIRPLAY",
    type=ConfigEntryType.ALERT,
    default_value=None,
    required=False,
    label="This player is known to have broken AirPlay support. "
    "Playback may fail or simply be silent. "
    "There is no workaround for this issue at the moment. \n"
    "If you already enforced AirPlay 2 on the player and it remains silent, "
    "this is one of the known broken models. Only remedy is to nag the manufacturer for a fix.",
)

BASE_PLAYER_FEATURES: Final[set[PlayerFeature]] = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.MULTI_DEVICE_DSP,
    PlayerFeature.VOLUME_SET,
}
