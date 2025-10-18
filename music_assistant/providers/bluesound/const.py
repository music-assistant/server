"""Constants for the Bluesound provider."""

from __future__ import annotations

from music_assistant_models.enums import PlaybackState, PlayerFeature

from music_assistant.models.player import PlayerSource

IDLE_POLL_INTERVAL = 30
PLAYBACK_POLL_INTERVAL = 10

PLAYER_FEATURES_BASE = {
    PlayerFeature.SET_MEMBERS,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.PAUSE,
    PlayerFeature.SELECT_SOURCE,
    PlayerFeature.NEXT_PREVIOUS,
    PlayerFeature.SEEK,
}

PLAYBACK_STATE_MAP = {
    "play": PlaybackState.PLAYING,
    "stream": PlaybackState.PLAYING,
    "stop": PlaybackState.IDLE,
    "pause": PlaybackState.PAUSED,
    "connecting": PlaybackState.IDLE,
}

PLAYBACK_STATE_POLL_MAP = {
    "play": PlaybackState.PLAYING,
    "stream": PlaybackState.PLAYING,
    "stop": PlaybackState.IDLE,
    "pause": PlaybackState.PAUSED,
    "connecting": "CONNECTING",
}

SOURCE_TIDAL = "Tidal"
SOURCE_AIRPLAY = "AirPlay"
SOURCE_SPOTIFY = "Spotify"
SOURCE_RADIOPARADISE = "RadioParadise"
SOURCE_TUNEIN = "TuneIn"
SOURCE_HTTP = "http"
SOURCE_BLUETOOTH = "Bluetooth"
SOURCE_TV = "HDMI ARC"

PLAYER_SOURCE_MAP = {
    SOURCE_HTTP: PlayerSource(
        id=SOURCE_HTTP,
        name="HTTP Stream",
        passive=True,
        can_play_pause=True,
        can_next_previous=False,
        can_seek=False,
    ),
    SOURCE_BLUETOOTH: PlayerSource(
        id=SOURCE_BLUETOOTH,
        name="Bluetooth",
        passive=True,
        can_play_pause=True,
        can_next_previous=False,
        can_seek=False,
    ),
    SOURCE_TV: PlayerSource(
        id=SOURCE_TV,
        name="HDMI ARC",
        passive=True,
        can_play_pause=False,
        can_next_previous=False,
        can_seek=False,
    ),
    SOURCE_AIRPLAY: PlayerSource(
        id=SOURCE_AIRPLAY,
        name="AirPlay",
        passive=True,
        can_play_pause=True,
        can_next_previous=False,
        can_seek=False,
    ),
    SOURCE_SPOTIFY: PlayerSource(
        id=SOURCE_SPOTIFY,
        name="Spotify",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_TIDAL: PlayerSource(
        id=SOURCE_TIDAL,
        name="Tidal",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=True,
    ),
    SOURCE_RADIOPARADISE: PlayerSource(
        id=SOURCE_RADIOPARADISE,
        name="Radio Paradise",
        passive=True,
        can_play_pause=True,
        can_next_previous=True,
        can_seek=False,
    ),
    SOURCE_TUNEIN: PlayerSource(
        id=SOURCE_TUNEIN,
        name="TuneIn",
        passive=True,
        can_play_pause=True,
        can_next_previous=False,
        can_seek=False,
    ),
}

POLL_STATE_STATIC = "static"
POLL_STATE_DYNAMIC = "dynamic"

MUSP_MDNS_TYPE = "_musp._tcp.local."
