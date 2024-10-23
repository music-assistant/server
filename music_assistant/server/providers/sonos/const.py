"""Constants for the Sonos (S2) provider."""

from __future__ import annotations

from aiosonos.api.models import PlayBackState as SonosPlayBackState

from music_assistant.common.models.enums import PlayerFeature, PlayerState

PLAYBACK_STATE_MAP = {
    SonosPlayBackState.PLAYBACK_STATE_BUFFERING: PlayerState.PLAYING,
    SonosPlayBackState.PLAYBACK_STATE_IDLE: PlayerState.IDLE,
    SonosPlayBackState.PLAYBACK_STATE_PAUSED: PlayerState.PAUSED,
    SonosPlayBackState.PLAYBACK_STATE_PLAYING: PlayerState.PLAYING,
}

PLAYER_FEATURES_BASE = {
    PlayerFeature.SYNC,
    PlayerFeature.VOLUME_MUTE,
    PlayerFeature.PAUSE,
    PlayerFeature.ENQUEUE,
}

SOURCE_LINE_IN = "line_in"
SOURCE_AIRPLAY = "airplay"
SOURCE_SPOTIFY = "spotify"
SOURCE_UNKNOWN = "unknown"
SOURCE_RADIO = "radio"

CONF_AIRPLAY_MODE = "airplay_mode"
