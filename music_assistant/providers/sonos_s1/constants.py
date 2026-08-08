"""Constants for Sonos S1 Player Provider."""

from __future__ import annotations

from music_assistant_models.enums import PlaybackState, PlayerFeature
from soco.core import (
    MUSIC_SRC_AIRPLAY,
    MUSIC_SRC_LINE_IN,
    MUSIC_SRC_RADIO,
    MUSIC_SRC_SPOTIFY_CONNECT,
    MUSIC_SRC_TV,
)

from music_assistant.models.player import PlayerSource

# Configuration Keys
CONF_NETWORK_SCAN = "network_scan"
CONF_HOUSEHOLD_ID = "household_id"

# Player Features
PLAYER_FEATURES = (
    PlayerFeature.PLAY_MEDIA,
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.ENQUEUE,
    PlayerFeature.GAPLESS_PLAYBACK,
    PlayerFeature.SELECT_SOURCE,
)

# Source Mapping
SOURCES_MAP = {
    MUSIC_SRC_LINE_IN: "Line-in",
    MUSIC_SRC_TV: "TV",
    MUSIC_SRC_RADIO: "Radio",
    MUSIC_SRC_SPOTIFY_CONNECT: "Spotify",
    MUSIC_SRC_AIRPLAY: "AirPlay",
}

SOURCE_AIRPLAY = "AirPlay"
SOURCE_LINEIN = "Line-in"
SOURCE_SPOTIFY_CONNECT = "Spotify Connect"
SOURCE_TV = "TV"

SOURCE_MAPPING = {
    MUSIC_SRC_AIRPLAY: SOURCE_AIRPLAY,
    MUSIC_SRC_TV: SOURCE_TV,
    MUSIC_SRC_LINE_IN: SOURCE_LINEIN,
    MUSIC_SRC_SPOTIFY_CONNECT: SOURCE_SPOTIFY_CONNECT,
}

LINEIN_SOURCES = (MUSIC_SRC_TV, MUSIC_SRC_LINE_IN)
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
    SOURCE_AIRPLAY: PlayerSource(
        id=SOURCE_AIRPLAY,
        name="AirPlay",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_SPOTIFY_CONNECT: PlayerSource(
        id=SOURCE_SPOTIFY_CONNECT,
        name="Spotify Connect",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
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
# S1 speakers apply a command a moment after acknowledging it, so the resulting state is
# read back with a short delay instead of trusting the response to the command itself.
COMMAND_POLL_DELAY = 2

# Position/Duration Keys
DURATION_SECONDS = "duration_in_s"
POSITION_SECONDS = "position_in_s"

# UID Constants
UID_PREFIX = "RINCON_"
UID_POSTFIX = "01400"
