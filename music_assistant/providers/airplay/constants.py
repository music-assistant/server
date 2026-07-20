"""Constants for the AirPlay provider."""

from __future__ import annotations

from dataclasses import replace
from enum import IntEnum
from typing import Final

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, ContentType, PlayerFeature
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_ENTRY_SYNC_ADJUST, INTERNAL_PCM_FORMAT

DOMAIN = "airplay"


class StreamingProtocol(IntEnum):
    """AirPlay streaming protocol versions."""

    RAOP = 1  # AirPlay 1 (RAOP)
    AIRPLAY2 = 2  # AirPlay 2


CONF_VOLUME_START: Final[str] = "volume_start"
CONF_PASSWORD: Final[str] = "password"
CONF_AP2PASSWORD: Final[str] = "ap2password"
CONF_IGNORE_VOLUME: Final[str] = "ignore_volume"
# Advanced per-device escape hatch: force the legacy RAOP protocol on an
# AirPlay-2-capable receiver whose AirPlay 2 implementation misbehaves.
CONF_FORCE_RAOP: Final[str] = "force_raop"
CONF_STORED_VOLUME: Final[str] = "stored_volume"
CONF_HIRES_PLAYBACK: Final[str] = "hires_playback"

AIRPLAY_DISCOVERY_TYPE: Final[str] = "_airplay._tcp.local."
RAOP_DISCOVERY_TYPE: Final[str] = "_raop._tcp.local."
DACP_DISCOVERY_TYPE: Final[str] = "_dacp._tcp.local."

# Fixed lead (ms) between starting the stream and the audible group start
# (--start-unix-ms means "the first sample is audible exactly at this instant"
# on every protocol path). Covers process spawn + connect/session setup plus
# the receiver-buffer pre-fill the binary does ahead of the audible start.
# The effective pre-fill is roughly lead - connect_time; the native AirPlay 2
# path needs a larger budget than RAOP because its pre-fill is paced (RAOP
# bursts its backlog and fills faster), so too short a lead intermittently
# clips the first fraction of a second on native receivers such as Sonos.
AIRPLAY_RAOP_SETUP_LEAD_MS: Final[int] = 1500
AIRPLAY_AP2_SETUP_LEAD_MS: Final[int] = 2500
# Late joiners keep a more conservative headroom: besides connecting, their
# pipeline must also be primed from the session's history buffer.
AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS: Final[int] = 2000

# Cover art is rendered to a local JPEG for the binary to embed (the binary
# does not fetch URLs). 512px keeps the SET_PARAMETER payload small while still
# looking sharp on speaker apps and the Apple TV now-playing screen.
AIRPLAY_ARTWORK_SIZE: Final[int] = 512

# Per-protocol credential storage keys
CONF_RAOP_CREDENTIALS: Final[str] = "raop_credentials"
CONF_AIRPLAY_CREDENTIALS: Final[str] = "airplay_credentials"

# Provider-level marker for the one-time reset of user-calibrated sync_adjust
# values from before the unified cliairplay binary, whose different timing model
# invalidates offsets calibrated against the old implementation.
CONF_SYNC_ADJUST_RESET_MARKER: Final[str] = "unified_binary_sync_adjust_reset"

# AirPlay serves the shared sync-adjust control as a non-advanced (always visible)
# setting: with the unified binary no longer auto-applying device-reported render
# latency, this is now the primary way to compensate a device wired to a TV / AV
# receiver / amplifier that adds its own audio delay. The AirPlay-scoped strings
# spell out the sign; the shared entry stays advanced for other providers.
CONF_ENTRY_SYNC_ADJUST_AIRPLAY = replace(CONF_ENTRY_SYNC_ADJUST, advanced=False)

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
