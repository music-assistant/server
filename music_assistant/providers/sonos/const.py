"""Constants for the Sonos (S2) provider."""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple

from aiosonos.api.models import PlayBackState as SonosPlayBackState
from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.models.player import PlayerSource

PLAYBACK_STATE_MAP = {
    SonosPlayBackState.PLAYBACK_STATE_BUFFERING: PlaybackState.PLAYING,
    SonosPlayBackState.PLAYBACK_STATE_IDLE: PlaybackState.IDLE,
    SonosPlayBackState.PLAYBACK_STATE_PAUSED: PlaybackState.PAUSED,
    SonosPlayBackState.PLAYBACK_STATE_PLAYING: PlaybackState.PLAYING,
}

PLAYER_FEATURES_BASE = {
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.PAUSE,
    PlayerFeature.ENQUEUE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.SEEK,
    PlayerFeature.SELECT_SOURCE,
    PlayerFeature.GAPLESS_PLAYBACK,
}

SOURCE_LINE_IN = "line_in"
SOURCE_AIRPLAY = "airplay"
SOURCE_SPOTIFY = "spotify"
SOURCE_UNKNOWN = "unknown"
SOURCE_TV = "tv"
SOURCE_RADIO = "radio"
# a Music Assistant session this server did not start, such as another server's
SOURCE_MUSIC_ASSISTANT = "music_assistant"

PLAYER_SOURCE_MAP = {
    SOURCE_LINE_IN: PlayerSource(
        id=SOURCE_LINE_IN,
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
    SOURCE_SPOTIFY: PlayerSource(
        id=SOURCE_SPOTIFY,
        name="Spotify",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_RADIO: PlayerSource(
        id=SOURCE_RADIO,
        name="Radio",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_MUSIC_ASSISTANT: PlayerSource(
        id=SOURCE_MUSIC_ASSISTANT,
        name="Music Assistant",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        # a Music Assistant queue offers no seeking on Sonos
        can_seek=False,
    ),
}

DEVICE_FEATURE_WAKEABLE = "WAKEABLE"

UNSUPPORTED_MODELS_NATIVE_ANNOUNCEMENTS = ("Play:1", "Play:3")
NON_HIRES_MODELS = (
    "Connect:Amp",
    "Table lamp",
)

# model lists as known to soco (used by the Home Assistant Sonos integration)
AMP_MODEL = "Amp"  # always drives a (RCA) subwoofer and has a subwoofer crossover
ULTRA_SOUNDBAR_MODELS = ("Arc Ultra", "Beam Ultra")  # speech enhancement in levels 1..4


class OptionRange(NamedTuple):
    """Value range of a numeric player option."""

    minimum: int
    maximum: int


EQ_RANGE = OptionRange(-10, 10)
BALANCE_RANGE = OptionRange(-100, 100)
AUDIO_DELAY_RANGE = OptionRange(0, 5)
DIALOG_LEVEL_RANGE = OptionRange(1, 4)
SUB_GAIN_RANGE = OptionRange(-15, 15)
SUB_CROSSOVER_RANGE = OptionRange(50, 110)
SURROUND_LEVEL_RANGE = OptionRange(-15, 15)
# player config: offer the options the speaker sends no events for, which are polled
CONF_SHOW_DEVICE_SETTINGS = "show_device_settings"

# UPnP requests to a speaker stop after this many timeouts in a row, until a reconnect:
# the player poll loop of all players waits for each request
UPNP_TIMEOUT_LIMIT = 5

# UPnP of the speakers (reference, unofficial: https://sonos.svrooij.io/services)
UPNP_PORT = 1400
UPNP_TIMEOUT = 10
RENDERING_CONTROL_SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"
DEVICE_PROPERTIES_SERVICE = "urn:schemas-upnp-org:service:DeviceProperties:1"
HT_CONTROL_SERVICE = "urn:schemas-upnp-org:service:HTControl:1"


class EqType(StrEnum):
    """EQ types of RenderingControl GetEQ/SetEQ; not every speaker supports each."""

    AUDIO_DELAY = "AudioDelay"
    DIALOG_LEVEL = "DialogLevel"
    MUSIC_SURROUND_LEVEL = "MusicSurroundLevel"
    SUB_CROSSOVER = "SubCrossover"
    SUB_ENABLED = "SubEnable"
    SUB_GAIN = "SubGain"
    SURROUND_ENABLED = "SurroundEnable"
    SURROUND_LEVEL = "SurroundLevel"
    SURROUND_MODE = "SurroundMode"  # 0 = ambient, 1 = full


class IrRepeaterState(StrEnum):
    """States of the IR repeater of a home theater speaker."""

    ON = "On"
    OFF = "Off"
    DISABLED = "Disabled"


# UPnP RenderingControl events, received on the stream server
EVENTS_PATH = "/sonos/notify"
# seconds a subscription is asked for; it is renewed halfway, by the poll
EVENT_SUBSCRIPTION_TIMEOUT = 1800
# poll interval (seconds) of the player options; with events coming in and no option that
# sends none offered, the poll only renews the subscription
OPTIONS_POLL_INTERVAL = 30
OPTIONS_EVENTS_POLL_INTERVAL = 300
# seconds to wait after an event before reading the options, so a slider moved in the
# Sonos app is read once
OPTIONS_EVENT_READ_DELAY = 1
# the RenderingControl state variables a player option follows
OPTION_EVENT_VARIABLES = frozenset(
    {
        "Bass",
        "Treble",
        "Loudness",
        "NightMode",
        "DialogLevel",
        "SpeechEnhanceEnabled",
        "AudioDelay",
        "SubEnabled",
        "SubGain",
        "SubCrossover",
        "SurroundEnabled",
        "SurroundLevel",
        "MusicSurroundLevel",
        "SurroundMode",
        "Volume_LF",
        "Volume_RF",
    }
)

# How much of the queue one itemWindow response describes. A speaker only asks for a new
# window once it runs out, so a shallow one leaves it reloading at a track boundary, which
# Sonos can fail. The items are addressed by queue position, so a track it cached before a
# queue edit plays whatever the queue holds at that position by then. The previous item
# keeps skip-back working. The speaker's own sizes are never exceeded.
PREVIOUS_ITEMS = 1
UPCOMING_ITEMS = 10
