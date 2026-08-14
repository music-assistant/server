"""
Enum types for the bose_soundtouch library.

Taken from https://github.com/captivus/bose-soundtouch/blob/master/src/bose_soundtouch/enums.py,
LICENSE: MIT
"""

from enum import StrEnum


class ArtStatus(StrEnum):
    """Status of album art image."""

    INVALID = "INVALID"
    SHOW_DEFAULT_IMAGE = "SHOW_DEFAULT_IMAGE"
    DOWNLOADING = "DOWNLOADING"
    IMAGE_PRESENT = "IMAGE_PRESENT"


class Key(StrEnum):
    """Remote control key values for the /key endpoint."""

    PLAY = "PLAY"
    PAUSE = "PAUSE"
    STOP = "STOP"
    PREV_TRACK = "PREV_TRACK"
    NEXT_TRACK = "NEXT_TRACK"
    THUMBS_UP = "THUMBS_UP"
    THUMBS_DOWN = "THUMBS_DOWN"
    BOOKMARK = "BOOKMARK"
    POWER = "POWER"
    MUTE = "MUTE"
    VOLUME_UP = "VOLUME_UP"
    VOLUME_DOWN = "VOLUME_DOWN"
    PRESET_1 = "PRESET_1"
    PRESET_2 = "PRESET_2"
    PRESET_3 = "PRESET_3"
    PRESET_4 = "PRESET_4"
    PRESET_5 = "PRESET_5"
    PRESET_6 = "PRESET_6"
    AUX_INPUT = "AUX_INPUT"
    SHUFFLE_OFF = "SHUFFLE_OFF"
    SHUFFLE_ON = "SHUFFLE_ON"
    REPEAT_OFF = "REPEAT_OFF"
    REPEAT_ONE = "REPEAT_ONE"
    REPEAT_ALL = "REPEAT_ALL"
    PLAY_PAUSE = "PLAY_PAUSE"
    ADD_FAVORITE = "ADD_FAVORITE"
    REMOVE_FAVORITE = "REMOVE_FAVORITE"
    INVALID_KEY = "INVALID_KEY"


class KeyState(StrEnum):
    """Key press/release state for the /key endpoint."""

    PRESS = "press"
    RELEASE = "release"


class PlayStatus(StrEnum):
    """Playback status from now_playing."""

    PLAY_STATE = "PLAY_STATE"
    PAUSE_STATE = "PAUSE_STATE"
    STOP_STATE = "STOP_STATE"
    BUFFERING_STATE = "BUFFERING_STATE"
    INVALID_PLAY_STATUS = "INVALID_PLAY_STATUS"


class SourceStatus(StrEnum):
    """Content source availability status."""

    UNAVAILABLE = "UNAVAILABLE"
    READY = "READY"


class AudioMode(StrEnum):
    """DSP audio mode for /audiodspcontrols."""

    DIRECT = "AUDIO_MODE_DIRECT"
    NORMAL = "AUDIO_MODE_NORMAL"
    DIALOG = "AUDIO_MODE_DIALOG"
    NIGHT = "AUDIO_MODE_NIGHT"
