"""Constants for the Sonos (S2) provider."""

from __future__ import annotations

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
}

UNSUPPORTED_MODELS_NATIVE_ANNOUNCEMENTS = ("Play:1", "Play:3")
NON_HIRES_MODELS = (
    "Play:1",
    "Play:3",
    "Connect",
    "Connect:Amp",
    "Table lamp",
)

# How much of the queue an itemWindow response describes. A speaker caches the window it
# fetched and only comes back for a new one once it nears the end of it, so a deep window is
# a deep stale cache - it would keep playing out of it for as many tracks as it holds, and
# the /version poll that would otherwise catch a change runs only every 10 minutes. Serving
# just the playing item and the one after it (the same current+next the other player
# providers get through enqueue_next_media) makes the speaker ask again for every track, so
# it can never be more than one track behind. The single previous item keeps skip-back
# working from the speaker itself. The sizes a speaker asks for are maxima, so serving fewer
# is within the contract.
PREVIOUS_ITEMS = 1
UPCOMING_ITEMS = 1
