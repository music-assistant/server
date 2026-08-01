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
# Storage-only marker (no config entry) set when the device rejected the stored
# password, so the player keeps asking for setup across restarts until a working
# password is entered.
CONF_PASSWORD_INVALID: Final[str] = "password_invalid"
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
# Late joiners anchor a low first guess, not a worst-case bound: a join START
# makes the binary verify receiver clock readiness and correct the anchor
# forward to it, advancing the queued content by the same (inaudible) amount.
# The floor only keeps that correction window open: it must clear the binary's
# verification arm window (AP2_CLOCK_VERIFY_MIN_WINDOW_MS, 1100 ms) plus a
# poll round and the START command latency.
AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS: Final[int] = 1500
# Anchor leads for a readiness-confirmed START (cold and warm alike), solo and
# group: the session only anchors after the binary confirmed the connection
# ([STATUS] connected) and the new audio flowing ([STATUS] audio), so a lead no
# longer guesses at setup or transcoder spin-up time. Both cover the receiver
# re-anchor (accepted down to ~150 ms in the flush-ladder measurements) plus
# the command's trip down the pipe; the group one also covers fanning the
# shared instant out to every member. The pipe margin is what keeps them
# workable: the binary rejects any instant inside its own 250 ms floor,
# measured from when IT reads the command, and corrects a miss to that floor
# plus another full lead — so a lead sitting exactly on the floor misses by the
# delivery time every time and turns a start into a group-wide re-anchor ladder.
AIRPLAY_START_LEAD_MS: Final[int] = 400
AIRPLAY_GROUP_START_LEAD_MS: Final[int] = 500
# Cold GROUP starts anchor further out: a receiver on a brand-new session
# still acquires its PTP slave lock (~1.7-2.3 s measured on Sonos) and cannot
# render at an anchor inside that window - it starts late and the group opens
# audibly out of sync. Warm re-anchors reuse a locked clock and keep the
# short leads above; solo cold starts have no sync partner to miss.
AIRPLAY_COLD_GROUP_START_LEAD_MS: Final[int] = 2500
# Margin added on top of a member's reported warm lead (the splice-timeline
# queue depth on Apple receivers) when anchoring a warm re-start: covers the
# command round-trips between the flush acks and the shared START so every
# member's skip target lands beyond its queued audio.
AIRPLAY_SPLICE_LEAD_MARGIN_MS: Final[int] = 150

# Delay (seconds) before automatically re-joining a group member whose
# cliairplay process died unexpectedly mid-session (e.g. the device rode out a
# network blackout longer than the binary's own keepalive tolerance). A single
# attempt keeps the behaviour predictable: it waits long enough for a short
# blackout to clear, and if the device is still gone the player is left idle.
# Staged retries can be reintroduced by adding entries to the tuple.
AIRPLAY_REJOIN_ATTEMPT_DELAYS: Final[tuple[int, ...]] = (5,)

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
# Observed on tvOS when an AirPlay password is set. Apple TVs keep PASSWORD_BIT
# raised at all times (it marks their onscreen-code capability, not a password),
# so this is the only flags-based password signal they give.
ATV_PASSWORD_BIT = 0x1000

# Provider setting: opt-in for the shared PTP daemon's per-packet timing trace
# (Announce/Sync/Follow_Up) when verbose logging is active. Off by default —
# the trace floods the log and only matters for clock-sync debugging.
CONF_VERBOSE_PTP_LOGGING: Final[str] = "verbose_ptp_logging"
