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

# Configuration Keys
CONF_NETWORK_SCAN = "network_scan"
CONF_HOUSEHOLD_ID = "household_id"

# Player Features
PLAYER_FEATURES = (
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.VOLUME_SET,
    PlayerFeature.ENQUEUE,
    PlayerFeature.GAPLESS_PLAYBACK,
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

# Subscription Settings
SUBSCRIPTION_TIMEOUT = 1200
SUBSCRIPTION_SERVICES = {
    "avTransport",
    "deviceProperties",
    "renderingControl",
    "zoneGroupTopology",
}

# Timing Constants
NEVER_TIME = 0
RESUB_COOLDOWN_SECONDS = 10.0

# Position/Duration Keys
DURATION_SECONDS = "duration_in_s"
POSITION_SECONDS = "position_in_s"

# UID Constants
UID_PREFIX = "RINCON_"
UID_POSTFIX = "01400"
