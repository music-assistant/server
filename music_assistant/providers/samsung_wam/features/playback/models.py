"""Models specific to playback."""

from enum import StrEnum


class WamSource(StrEnum):
    """Enum for WAM player sources."""

    WIFI = "Wi-Fi"
    BLUETOOTH = "Bluetooth"
    AUX = "AUX"
    OPTICAL = "Optical"
    TV_SOUNDCONNECT = "TV SoundConnect"


class WamPlaybackState(StrEnum):
    """Enum for WAM internal playback states."""

    PLAY = "play"
    PAUSE = "pause"
    STOP = "stop"
