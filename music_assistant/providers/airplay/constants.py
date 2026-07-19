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


CONF_VOLUME_START: Final[str] = "volume_start"
CONF_PASSWORD: Final[str] = "password"
CONF_AP2PASSWORD: Final[str] = "ap2password"
CONF_IGNORE_VOLUME: Final[str] = "ignore_volume"
CONF_AIRPLAY_PROTOCOL: Final[str] = "airplay_protocol"
CONF_STORED_VOLUME: Final[str] = "stored_volume"
CONF_HIRES_PLAYBACK: Final[str] = "hires_playback"

AIRPLAY_DISCOVERY_TYPE: Final[str] = "_airplay._tcp.local."
RAOP_DISCOVERY_TYPE: Final[str] = "_raop._tcp.local."
DACP_DISCOVERY_TYPE: Final[str] = "_dacp._tcp.local."

# Fixed lead (ms) between starting the stream and the audible group start
# (--start-unix-ms means "the first sample is audible exactly at this instant"
# on every protocol path). Covers process spawn + connect/session setup with
# margin; the binary fills the receiver's buffer ahead of the audible start
# automatically, so the start cannot underrun.
AIRPLAY_SETUP_LEAD_MS: Final[int] = 2500
# The --latency override is only passed to the binary when the user explicitly
# configured it; 0 means automatic (the binary's AirPlay-standard 2000 ms
# default, clamped to the device-reported buffering window).
AIRPLAY_LATENCY_AUTO: Final[int] = 0
AIRPLAY_LATENCY_MAX_MS: Final[int] = 5000

# Per-protocol credential storage keys
CONF_RAOP_CREDENTIALS: Final[str] = "raop_credentials"
CONF_AIRPLAY_CREDENTIALS: Final[str] = "airplay_credentials"

# Provider-level marker for the one-time reset of user-calibrated sync_adjust
# values from before the unified cliairplay binary (which auto-compensates
# device-reported render latency, invalidating old calibrations).
CONF_SYNC_ADJUST_RESET_MARKER: Final[str] = "unified_binary_sync_adjust_reset"

# Some RAOP models require a higher than default 1000ms buffer to prevent stuttering
CONF_RAOP_LATENCY: Final[str] = "airplay_latency"

# Pairing action keys
CONF_ACTION_START_PAIRING: Final[str] = "start_pairing"
CONF_ACTION_FINISH_PAIRING: Final[str] = "finish_pairing"
CONF_ACTION_RESET_PAIRING: Final[str] = "reset_pairing"
CONF_PAIRING_PIN: Final[str] = "pairing_pin"
CONF_PAIRING_PASSWORD: Final[str] = "pairing_password"
BACKOFF_TIME_LOWER_LIMIT: Final[int] = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT: Final[int] = 300  # Five minutes

FALLBACK_VOLUME: Final[int] = 20
AIRPLAY_VOLUME_MUTE: Final[float] = -144.0

AIRPLAY_FLOW_PCM_FORMAT = AudioFormat(
    content_type=INTERNAL_PCM_FORMAT.content_type,
    sample_rate=44100,
    bit_depth=INTERNAL_PCM_FORMAT.bit_depth,
)
AIRPLAY_PCM_FORMAT = AudioFormat(
    content_type=ContentType.from_bit_depth(16), sample_rate=44100, bit_depth=16
)
# Sample rates advertised when the per-player hi-res option is enabled (AirPlay 2
# native flow only). At 24-bit the cliairplay binary expects raw s32le on stdin
# and truncates to 24-bit ALAC internally.
AIRPLAY_HIRES_SAMPLE_RATES: Final[list[tuple[int, int]]] = [(44100, 24), (48000, 24)]

BROKEN_AIRPLAY_MODELS = (
    # Samsung has been repeatedly being reported as having issues with AirPlay (raop and AP2)
    # Samsung will work with AirPlay2 once PTP timing is implemented for the MA build
    ("Samsung", "*"),
)

AIRPLAY_2_DEFAULT_MODELS = (
    # Models that are known to work better with AirPlay 2 protocol instead of RAOP
    # These use the translated/friendly model names from get_model_info()
    # Both fields support fnmatch-style wildcards and match case-insensitively.
    ("Ubiquiti Inc.", "*"),
    ("LG Electronics", "*"),
    # JBL models that advertise RAOP but reject the RAOP session.
    # The model name comes from the am mDNS property on these devices.
    ("*", "JBL BAR 1300"),
    ("*", "JBL BAR 300"),
    ("*", "JBL CHARGE 5*"),
)

BROKEN_AIRPLAY_WARN = ConfigEntry(
    key="BROKEN_AIRPLAY",
    type=ConfigEntryType.ALERT,
    default_value=None,
    required=False,
)

BASE_PLAYER_FEATURES: Final[set[PlayerFeature]] = {
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.MULTI_DEVICE_DSP,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.VOLUME_MUTE,
}


PIN_REQUIRED = 0x8
PASSWORD_BIT = 0x80
LEGACY_PAIRING_BIT = 0x200
