"""Constants for Sonos S1 Player Provider."""

from __future__ import annotations

from music_assistant_models.enums import PlaybackState, PlayerFeature
from soco.core import MUSIC_SRC_LINE_IN, MUSIC_SRC_TV

from music_assistant.models.player import PlayerSource

# Configuration Keys
CONF_NETWORK_SCAN = "network_scan"
CONF_HOUSEHOLD_ID = "household_id"

# Player Features
# PAUSE is advertised unconditionally: whether a speaker can pause depends on what it has
# loaded rather than on the model, so pause() reads that back from the speaker itself and
# falls back to stop when it is refused.
# The volume features are deliberately absent here: they depend on the individual speaker
# and are added by SonosPlayer.
PLAYER_FEATURES = (
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.PAUSE,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.ENQUEUE,
    PlayerFeature.GAPLESS_PLAYBACK,
    PlayerFeature.SELECT_SOURCE,
)

# Source Mapping
SOURCE_LINEIN = "Line-in"
SOURCE_TV = "TV"

# Line-in and TV are the only sources this provider can report or switch to.
LINEIN_SOURCE_MAPPING = {
    MUSIC_SRC_TV: SOURCE_TV,
    MUSIC_SRC_LINE_IN: SOURCE_LINEIN,
}

LINEIN_SOURCE_IDS = (SOURCE_TV, SOURCE_LINEIN)

PLAYER_SOURCE_MAP = {
    SOURCE_LINEIN: PlayerSource(
        id=SOURCE_LINEIN,
        name="Line-in",
        passive=False,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
    SOURCE_TV: PlayerSource(
        id=SOURCE_TV,
        name="TV",
        passive=False,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
}

# Playback State Mapping
PLAYBACK_STATE_MAP = {
    "PLAYING": PlaybackState.PLAYING,
    "PAUSED_PLAYBACK": PlaybackState.PAUSED,
    "STOPPED": PlaybackState.IDLE,
    "TRANSITIONING": PlaybackState.PLAYING,
}

# Sonos State Constants
SONOS_STATE_PLAYING = "PLAYING"
SONOS_STATE_TRANSITIONING = "TRANSITIONING"

POLL_INTERVAL = 5
# A speaker that reports TRANSITIONING carries no usable transport state (the coordinator of a
# group that is still forming reports it for several seconds), so that report is discarded. It is
# watched closely until it reports a usable state again, instead of leaving the player stale for a
# full poll interval.
TRANSITION_POLL_INTERVAL = 1

# Subscription Settings
SUBSCRIPTION_TIMEOUT = 1200
SUBSCRIPTION_SERVICES = {
    "avTransport",
    "deviceProperties",
    "renderingControl",
    "zoneGroupTopology",
}

# Timing Constants
DISCOVERY_INTERVAL = 1800
NEVER_TIME = 0
RESUB_COOLDOWN_SECONDS = 10.0
# S1 speakers often drop or delay control requests while busy, so a single failed request
# is not enough to mark a speaker offline. A failing ping is only trusted once events and
# polls have both been silent for this long.
AVAILABILITY_TIMEOUT = 60.0
# S1 speakers apply a command a moment after acknowledging it, so the resulting state is
# read back with a short delay instead of trusting the response to the command itself.
COMMAND_POLL_DELAY = 2

# Position/Duration Keys
DURATION_SECONDS = "duration_in_s"
POSITION_SECONDS = "position_in_s"

# UID Constants
UID_PREFIX = "RINCON_"
UID_POSTFIX = "01400"
