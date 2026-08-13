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


class ClockReadiness(StrEnum):
    """
    How a receiver's clock readiness resolved for an anchor decision.

    Only PROJECTED carries an instant; the rest all mean "anchor on the lead
    alone", but for very different reasons - one is a device that will not play
    at all, and treating them alike hides it.
    """

    # The binary projected when the receiver's clock becomes usable.
    PROJECTED = "projected"
    # NTP timing: there is no receiver clock to wait for.
    NOT_APPLICABLE = "not_applicable"
    # The receiver never answered our PTP clock and will render silence.
    STALLED = "stalled"
    # Nothing arrived within the wait: a slow device (retryable) or a receiver
    # whose readiness went unreported.
    UNREPORTED = "unreported"


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

# Lower bound for a late joiner's anchor, and the whole anchor whenever the
# binary reports no readiness projection ([STATUS] clock_ready). A joiner cannot
# honour an instant in the past, and that second case has no device evidence at
# all, so this carries it the whole way: the command's trip to the binary, the
# binary's own 250 ms floor, and the receiver seating the anchor. The value is
# field-proven, not derived from those.
# It is NOT headroom for the binary's post-commit clock verification, which
# arms only when the receiver has still not probed by the time it reads the
# START, and only for an anchor that clears the receiver queue depth plus
# 500 ms - past ~2 s of effective depth, an anchor this close leaves no room.
AIRPLAY_LATE_JOIN_MIN_HEADROOM_MS: Final[int] = 2500
# How long a group start, a join or the Sendspin bridge waits for the binary's
# first receiver-clock projection ([STATUS] clock_ready with state=probing|
# ready). The binary emits its first line right after [STATUS] connected and
# refreshes it about every 250 ms, and the projection exists from the receiver's
# first probe (~1078 ms after connect on a cold device), so this only has to
# cover a slower device before giving up and anchoring on the lead alone.
# Independent of the binary's own stall report (state=stalled), which is a
# slower, higher-confidence diagnosis of a receiver that never answers at all: a
# wait that times out here has simply run out of planning time and falls back,
# while a stall says the speaker will not play. Keep them apart - tightening the
# stall report to meet this deadline would trade the margin that keeps it free
# of false alarms.
AIRPLAY_CLOCK_READY_TIMEOUT_MS: Final[int] = 2500
# Lead added on top of a reported readiness instant, wherever one is waited for.
# The binary refuses to place an anchor inside its own 250 ms floor, measured
# from when IT reads the START command rather than when the server sends it (the
# same trap as AIRPLAY_START_LEAD_MS), so the lead carries that floor plus 250 ms
# for the command reaching the binary and for the convergence error of a
# projection made from the receiver's very first probe.
AIRPLAY_CLOCK_READY_LEAD_MS: Final[int] = 500
# Default receiver buffer depth per device family: (manufacturer wildcard,
# model wildcard, firmware wildcard) -> depth in ms, matched case-insensitively
# in order, first match wins; unmatched devices stay on Automatic (the binary's
# stock depth). LinkPlay pipelines starve at the stock depth - silent renderer
# behind a perfectly healthy session - so their queue is deepened, at the cost
# of slower warm seeks (the depth IS the audible latency of a seek or skip) and,
# past ~2 s of effective depth, of the binary's post-commit clock verification,
# which stops arming once the depth plus 500 ms outruns a late joiner's anchor.
# Extend the table as field reports identify more starving devices.
AIRPLAY_BUFFER_DEPTH_DEFAULTS: Final[tuple[tuple[str, str, str, int], ...]] = (
    # The newer LinkPlay platform names Linkplay as the manufacturer (WiiM, ...).
    # 1750 ms is what a WiiM needs once it is also master of a native multiroom
    # group.
    ("linkplay*", "*", "*", 1750),
    # The older LinkPlay platform ships under OEM brands (Edifier, ...) but
    # marks the platform in its firmware string. It starves far deeper: an
    # Edifier MS50A stays silent at 2250 ms and renders from 2500 ms, which is
    # the 2250 ms pipeline the same device declares as its RAOP latency plus the
    # binary's delivery margin. Every shallower value - including the 1750 ms
    # this row used to inherit from the row above - is below what the device
    # itself asks for.
    ("*", "*", "p20.linkplay.*", 2500),
)
# Per-player override of the splice receiver-queue depth in ms (0 = automatic).
CONF_BUFFER_DEPTH: Final[str] = "buffer_depth"
# How long a plain (non-join) START waits for the binary's [STATUS] started ack.
# Nothing holds that ack back, so the window only has to cover the command's trip
# down the pipe and the answer coming back - unlike a join's ack below, which is
# withheld whenever the receiver clock verification arms.
AIRPLAY_START_ACK_TIMEOUT_MS: Final[int] = 2000
# How long a join START waits for the binary's [STATUS] started ack. That ack is
# held back whenever the clock verification above arms, so the window must
# cover the verification arm window plus a poll round on top of the commanded
# anchor (which bounds the verification), where a plain START acks within the
# command round-trip. On timeout the server falls back to trusting the commanded
# instant, so a window shorter than the binary's verification silently maps the
# joiner's content onto an instant the binary never used.
AIRPLAY_JOIN_START_ACK_TIMEOUT_MS: Final[int] = 5000
# How far the content a corrected anchor actually cut may fall short of the cut
# it asked for before the reported media position is re-based and the shortfall
# reported. The binary derives the cut it took from the bytes it discarded, so a
# few ms of byte quantization is expected and correcting for it would only
# jitter the base; anything larger means the cut really did end early (the input
# ran out inside it, or a teardown settled it) and the position is over-advanced
# by that much for the rest of the anchor.
AIRPLAY_CONTENT_CUT_TOLERANCE_MS: Final[int] = 20
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

# Floor for the late-join PCM ring, which has to hold every sample between the
# audible position and the write head: a joiner's anchor maps onto content the
# group was already fed but has not played yet. That distance - the write-head
# lead - is the sum of every buffer between the session's byte counter and the
# speaker, and it is far larger than the binary's own ring alone:
#   ~5.7 s  the per-member ffmpeg and the pipes around it (measured 5.2 s
#           steady / 5.7 s peak at 44.1 kHz/16-bit, which is the worst case in
#           SECONDS - the same buffers hold ~4.3 s at the 32-bit hi-res carrier
#           rate, and low-delay ffmpeg flags do not shrink them)
#   ~4.0 s  the cliairplay ring: the binary's own lead (2000 ms default,
#           clamped to the device-reported window) plus its 2 s of slack
#   ~2.0 s  the receiver's own buffer
# ~11.7 s in total, against 8.9-11.0 s measured across native AirPlay 2
# sessions (Apple TV and Sonos, joining in both directions). This floor is that
# sum rounded up, and it is only a floor: the lead is measured per session and
# the ring grows to match, because a receiver that reports a wider lead window
# or buffers more deeply moves the sum with nothing to announce it.
AIRPLAY_LATE_JOIN_RING_MIN_SECONDS: Final[float] = 12.0
# Grown on top of the largest lead a session has actually shown, so a lead that
# drifts up between two joins cannot clip the oldest sample a joiner needs.
AIRPLAY_LATE_JOIN_RING_MARGIN_SECONDS: Final[float] = 2.0
# Hard bound on the ring, which is per session (one ring feeds every member) and
# costs byte_rate x seconds. Bounded in BYTES rather than seconds on purpose:
# the seconds the ring must hold are largest at the LOWEST byte rate (see the
# measurements above), so a single byte bound buys ~35 s at 44.1 kHz/16-bit -
# three times the measured lead, where the seconds are actually needed - and
# still ~16 s at the 32-bit hi-res carrier, where the pipeline holds fewer
# seconds anyway. 6 MiB per playing group is a few percent of the server's idle
# footprint.
AIRPLAY_LATE_JOIN_RING_MAX_BYTES: Final[int] = 6 * 1024 * 1024

# Delay (seconds) before automatically re-joining a group member whose
# cliairplay process died unexpectedly mid-session (e.g. the device rode out a
# network blackout longer than the binary's own keepalive tolerance). A single
# attempt keeps the behaviour predictable: it waits long enough for a short
# blackout to clear, and if the device is still gone the player is left idle.
# Staged retries can be reintroduced by adding entries to the tuple.
AIRPLAY_REJOIN_ATTEMPT_DELAYS: Final[tuple[int, ...]] = (5,)

# Shared audible instant for a native announcement over a live stream: now +
# the largest member span + this margin. A member can only mix the clip into
# audio it has not delivered yet, and its span (warm_lead_ms on the Apple
# splice timeline, the reported lead_ms otherwise) is how far its delivery
# head runs ahead of the audible position - so the earliest instant EVERY
# member can honor lies one max-span out. The margin covers fanning the
# command out over the pipes and the per-member arm processing, so one shared
# instant stays feasible for all members.
AIRPLAY_ANNOUNCE_AT_MARGIN_MS: Final[int] = 300
# Span assumed for a member whose binary reported neither a warm lead nor a
# device lead (both read 0 = unreported): the binary's own default playback
# lead, which bounds how far its delivery head can run ahead.
AIRPLAY_ANNOUNCE_FALLBACK_SPAN_MS: Final[int] = 2000
# Music gain (dB) under the clip while it plays; the binary ramps the duck in
# and out itself. <= -60 mutes the music entirely. -18 dB puts the music
# clearly in the background under speech (-12 was field-judged too shallow).
AIRPLAY_ANNOUNCE_DUCK_DB: Final[int] = -18
# Silence appended to every announcement clip file. The binary holds the duck
# for the whole file, so this keeps the music ducked past the announcement -
# the volume restore lands inside this cushion instead of racing the duck's
# 200 ms tail ramp (a restore that lands after the ramp plays a moment of
# full-level music at the still-bumped device volume).
AIRPLAY_ANNOUNCE_DUCK_TAIL_S: Final[float] = 1.0
# On top of the lead to the commanded instant: how long to wait for a member's
# announce_started before treating that member as not announcing. An outdated
# binary silently ignores the unknown command, so this bounded wait is also
# what detects that and routes the announcement to the fallback path.
AIRPLAY_ANNOUNCE_STARTED_TIMEOUT_MS: Final[int] = 3000
# On top of the clip's audible end: how long to wait for announce_done. The
# wait stays bounded because a queue that ends mid-clip emits its eof, which
# ends the status stream while the clip still plays out over the drain.
AIRPLAY_ANNOUNCE_DONE_TIMEOUT_MS: Final[int] = 5000
# Pad after the clip's audible end before the pre-announcement volume is
# restored: covers the jitter between the acked instant and true audibility.
AIRPLAY_ANNOUNCE_VOLUME_RESTORE_PAD_MS: Final[int] = 500
# Delay of the announcement-volume bump past the clip's audible start: a bump
# that lands early (a receiver playing out later than the reported instant)
# raises the still-playing music, so it is biased into the clip where the duck
# ramp masks it - the pre-announce chime covers the first moments anyway.
AIRPLAY_ANNOUNCE_VOLUME_BUMP_DELAY_MS: Final[int] = 300
# The AirPlay volume parameter is linear dB: 0..100 maps onto -30..0 dB on
# every flow (libraop raopcl_float_volume, reused verbatim by the native AP2
# SET_PARAMETER path), so one volume point is exactly 0.3 dB of output. This
# makes the announcement volume bump's effect on the music bed computable, and
# the duck is deepened by the same amount to keep the music's perceived level
# at the configured duck depth.
AIRPLAY_VOLUME_DB_PER_POINT: Final[float] = 0.3
# Drain margin for a dedicated announcement session: covers the receiver
# playing out its buffered audio after the clip's last byte was fed.
AIRPLAY_ANNOUNCE_SESSION_DRAIN_S: Final[float] = 2.0

# Cover art is rendered to a local JPEG for the binary to embed (the binary
# does not fetch URLs). 512px keeps the SET_PARAMETER payload small while still
# looking sharp on speaker apps and the Apple TV now-playing screen.
AIRPLAY_ARTWORK_SIZE: Final[int] = 512
# How long a track-change metadata push waits for that render, so the artwork
# can ride the SENDMETA bundle and the receiver rewrites its now-playing state
# once instead of twice (bare replace, then artwork). A render that misses the
# budget is not abandoned: it keeps running and is delivered with the
# stand-alone ARTWORK command once it completes.
AIRPLAY_ARTWORK_RENDER_TIMEOUT: Final[float] = 1.5
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
    PlayerFeature.PLAY_ANNOUNCEMENT,
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
# Bound on how many of those promoted lines the shared PTP daemon may produce per
# window before the rest are counted instead of logged. The markers above are
# deliberately broad, and "error" is ordinary vocabulary in clock telemetry
# (offset error, path delay error), so with the daemon's per-packet trace running
# at ~10 lines/s a single matching line would otherwise fill the log at WARNING.
# A burst still gets through, which is what a real one-shot daemon failure is.
PTP_DAEMON_WARN_BURST: Final[int] = 5
PTP_DAEMON_WARN_WINDOW: Final[float] = 60.0
