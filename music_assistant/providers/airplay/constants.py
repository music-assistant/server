"""Constants for the AirPlay provider."""

from __future__ import annotations

from dataclasses import replace
from enum import IntEnum, StrEnum
from typing import Final

from music_assistant_models.enums import ContentType, PlayerFeature
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_ENTRY_SYNC_ADJUST

DOMAIN = "airplay"


class StreamingProtocol(IntEnum):
    """AirPlay streaming protocol versions."""

    RAOP = 1  # AirPlay 1 (RAOP)
    AIRPLAY2 = 2  # AirPlay 2


class AirPlayRemoteCommand(StrEnum):
    """Transport commands received from an AirPlay receiver."""

    PLAY = "play"
    PAUSE = "pause"
    PLAY_PAUSE = "play_pause"
    NEXT = "next"
    PREVIOUS = "previous"


CONF_VOLUME_START: Final[str] = "volume_start"
CONF_PASSWORD: Final[str] = "password"
CONF_IGNORE_VOLUME: Final[str] = "ignore_volume"
CONF_ENCRYPTION: Final[str] = "encryption"
# Advanced per-device escape hatch: force the legacy RAOP protocol on an
# AirPlay-2-capable receiver whose AirPlay 2 implementation misbehaves.
CONF_FORCE_RAOP: Final[str] = "force_raop"
CONF_STORED_VOLUME: Final[str] = "stored_volume"
CONF_COMPANION_CREDENTIALS: Final[str] = "companion_credentials"
CONF_MRP_CREDENTIALS: Final[str] = "mrp_credentials"
CONF_NATIVE_MRP_CREDENTIALS: Final[str] = "native_mrp_credentials"

# Bundle id of the Music Assistant tvOS dashboard app, launched over Companion on
# eligible Apple TVs (see tvos/docs/launch-contract.md).
TVOS_APP_BUNDLE_ID: Final[str] = "io.music-assistant.tvos"

AIRPLAY_DISCOVERY_TYPE: Final[str] = "_airplay._tcp.local."
COMPANION_DISCOVERY_TYPE: Final[str] = "_companion-link._tcp.local."
MRP_DISCOVERY_TYPE: Final[str] = "_mediaremotetv._tcp.local."
RAOP_DISCOVERY_TYPE: Final[str] = "_raop._tcp.local."
DACP_DISCOVERY_TYPE: Final[str] = "_dacp._tcp.local."

# Setup lead (ms) advertised to externally timed sources such as Sendspin.
# It covers process spawn, connect/session setup and receiver pre-fill before
# the commanded audible instant. Native AirPlay 2 needs a larger budget than
# RAOP because its pre-fill is paced.
AIRPLAY_RAOP_SETUP_LEAD_MS: Final[int] = 1500
AIRPLAY_AP2_SETUP_LEAD_MS: Final[int] = 2500
# Late joiners keep a more conservative headroom: besides connecting, their
# pipeline must also be primed from the session's history buffer.
AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS: Final[int] = 2000
# Anchor lead for a readiness-confirmed START (cold and warm alike): the
# session only anchors after the binary confirmed the connection ([STATUS]
# connected) and the new audio flowing ([STATUS] audio), so the lead no longer
# guesses at setup or transcoder spin-up time. It covers just the receiver
# re-anchor (accepted down to ~150 ms in the flush-ladder measurements; the
# binary clamps below its own 250 ms floor) plus, for groups, fanning the
# shared instant out to every member.
AIRPLAY_START_LEAD_MS: Final[int] = 250
AIRPLAY_GROUP_START_LEAD_MS: Final[int] = 500

# Cover art is rendered to a local JPEG for the binary to embed (the binary
# does not fetch URLs). 512px keeps the SET_PARAMETER payload small while still
# looking sharp on speaker apps and the Apple TV now-playing screen.
AIRPLAY_ARTWORK_SIZE: Final[int] = 512
EXTERNAL_ARTWORK_PATH_PREFIX: Final[str] = "external_artwork"

# Per-protocol credential storage keys
CONF_RAOP_CREDENTIALS: Final[str] = "raop_credentials"
CONF_AIRPLAY_CREDENTIALS: Final[str] = "airplay_credentials"

# AirPlay serves the shared sync-adjust control as a non-advanced (always visible)
# setting: the binary handles lead/buffer automatically and does not apply
# device-reported render latency, so sync_adjust is the primary way to compensate
# a device wired to a TV / AV receiver / amplifier that adds its own audio delay.
# The AirPlay-scoped strings spell out the sign; the shared entry stays advanced
# for other providers.
CONF_ENTRY_SYNC_ADJUST_AIRPLAY = replace(CONF_ENTRY_SYNC_ADJUST, advanced=False)

# Interactive setup-flow input keys (transient PIN/password form fields and the
# optional "set up now?" choice for the control pairing steps).
CONF_PAIRING_PIN: Final[str] = "pairing_pin"
CONF_PAIRING_PASSWORD: Final[str] = "pairing_password"
CONF_COMPANION_PAIRING_PIN: Final[str] = "companion_pairing_pin"
CONF_MRP_PAIRING_PIN: Final[str] = "mrp_pairing_pin"
CONF_PAIR_NOW: Final[str] = "pair_now"
BACKOFF_TIME_LOWER_LIMIT: Final[int] = 15  # seconds
BACKOFF_TIME_UPPER_LIMIT: Final[int] = 300  # Five minutes

FALLBACK_VOLUME: Final[int] = 20
AIRPLAY_VOLUME_MUTE: Final[float] = -144.0

AIRPLAY_PCM_FORMAT = AudioFormat(
    content_type=ContentType.from_bit_depth(16), sample_rate=44100, bit_depth=16
)
# Sample rates advertised for a receiver that supports 24-bit (AirPlay 2 flow
# only). At 24-bit the cliairplay binary expects raw s32le on stdin and truncates
# to 24-bit ALAC internally.
AIRPLAY_HIRES_SAMPLE_RATES: Final[list[tuple[int, int]]] = [(44100, 24), (48000, 24)]

# Bits in the audioFormat bit space (shared with the receiver's /info format
# tables) that mark 24-bit ALAC: 44.1 kHz and 48 kHz respectively. Receivers
# understate these - the Apple TV lists them for its buffered stream only, yet
# renders them fine on the realtime stream - so a device advertising either bit
# on either stream is treated as 24-bit capable.
AIRPLAY_HIRES_AUDIO_FORMATS: Final[int] = (1 << 19) | (1 << 21)

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
