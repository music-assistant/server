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
# Lower bound for a late joiner's anchor, and the whole anchor whenever the
# binary reports no readiness projection ([STATUS] clock_ready). It has to keep
# the binary's own clock verification viable without any device evidence: that
# verification gives up 850 ms before the anchor, and a cold receiver was
# measured taking 1078 ms after connect to send its first clock probe, so an
# anchor below 1078 + 850 = ~1.93 s can close the window before a single probe
# arrives - the joiner then seats on a cold clock and lands audibly behind.
# 2500 ms clears that by ~570 ms, the margin the field-proven value carried.
AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS: Final[int] = 2500
# How long a join waits for the binary's first receiver-clock projection
# ([STATUS] clock_ready with state=probing|ready). The binary emits its first
# line right after [STATUS] connected and refreshes it about every 250 ms, and
# the projection exists from the receiver's first probe (~1078 ms after connect
# on a cold device), so this only has to cover a slower device before giving up
# and anchoring on the floor above. Independent of the binary's own stall
# report (state=stalled), which is a slower, higher-confidence diagnosis of a
# receiver that never answers at all: a join that times out here has simply run
# out of planning time and falls back, while a stall says the speaker will not
# play. Keep them apart - tightening the stall report to meet this deadline
# would trade the margin that keeps it free of false alarms.
AIRPLAY_JOIN_CLOCK_READY_TIMEOUT_MS: Final[int] = 2500
# Lead added on top of a reported readiness instant when anchoring a join. The
# binary refuses to place an anchor inside its own 250 ms floor, measured from
# when IT reads the START command rather than when the server sends it (the same
# trap as AIRPLAY_START_LEAD_MS, see PR #5208), so the lead carries that floor
# plus 250 ms for the command reaching the binary and for the convergence error
# of a projection made from the receiver's very first probe.
AIRPLAY_JOIN_CLOCK_READY_LEAD_MS: Final[int] = 500
# How long a join START waits for the binary's [STATUS] started ack. That ack is
# held back until the clock verification above resolves, so the window must
# cover the verification arm window plus a poll round on top of the commanded
# anchor (which bounds the verification), where a plain START acks within the
# command round-trip. On timeout the server falls back to trusting the commanded
# instant, so a window shorter than the binary's verification silently maps the
# joiner's content onto an instant the binary never used.
AIRPLAY_JOIN_START_ACK_TIMEOUT_MS: Final[int] = 5000
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

# The cliairplay binary tags no log levels on its output, so a genuine problem is
# recognised by keyword and promoted to a warning that stays visible at normal levels.
CLI_PROBLEM_MARKERS: Final[tuple[str, ...]] = ("error", "cannot", "failed", "unable")
